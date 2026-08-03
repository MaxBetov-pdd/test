# ICH A12/A13 on Windows

Это экспериментальный Windows-порт хостовой части проекта. Команды `status`,
`boot` и `iproxy` работают нативно через Python, libusb и Apple Mobile Device
Service. Скачивание IPSW, разбор IMG4, патчи iBoot/kernel и trustcache также
перенесены на Windows.

Оставшееся ограничение: порт пока не умеет надёжно расширять и записывать APFS
RestoreRamDisk. Для завершения `build` нужен заранее расширенный raw APFS-образ
с внедрённым SSH. Неизменённый stock ramdisk сборщик отклоняет.

Проект предназначен для исследования собственных устройств. Это не jailbreak
и не средство обхода Activation Lock, SEP или пароля устройства.

## Требования

- Windows 10/11 x64;
- Python 3.10 или новее;
- RP2350 с [usbliter8](https://github.com/prdgmshift/usbliter8) и устройство в
  `PWND: usbliter8` DFU;
- Apple Devices из Microsoft Store либо актуальный iTunes — требуется для
  `iproxy` после загрузки ramdisk;
- предпочтительно USB-A → Lightning без USB-C-переходника.

## Установка

Откройте PowerShell в корне проекта:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\windows\ich.ps1 setup
.\windows\ich.ps1 status
```

Если `status` сообщает `Access denied` или не видит подключённый DFU, назначьте
драйвер WinUSB через Zadig только текущему Apple DFU-интерфейсу `05AC:1227`.
При необходимости то же делается отдельно для Recovery PID `05AC:1280–1283`.
Не заменяйте драйвер обычного normal-mode Apple Mobile Device: он нужен
Apple Mobile Device Service и `iproxy`.

Ожидаемый статус перед сборкой и загрузкой:

```text
MODE: DFU
CPID: 0x8020       # A12; для A13 — 0x8030
PWND: usbliter8
```

## Сборка

С подключённым pwned DFU сборщик сам определит product/board/CPID:

```powershell
.\windows\ich.ps1 build --version <iOS-version>
```

Без устройства параметры можно передать явно, например для iPhone XR:

```powershell
.\windows\ich.ps1 build --version <iOS-version> `
  --product iPhone11,8 --board n841ap --cpid 0x8020
```

Первый запуск без `--prepared-ramdisk` завершится кодом `4`. Это ожидаемый
двухэтапный режим: он сохранит `ramdisk.stock.dmg`, создаст
`ramdisk-job.json` и напечатает постоянный путь в `prepared-ramdisk\`, куда
нужно положить подготовленный образ.

Требования из job-файла:

- raw APFS-контейнер размером 256–280 MiB;
- `resources/ssh.tar.gz` распакован в корень с сохранением POSIX modes и symlinks;
- `/usr/local/bin/restored_external` заменён файлом из `resources` и имеет mode
  `0755`;
- `/usr/bin/mount_ich` имеет mode `0755`.

На Windows пока нет используемого этим портом надёжного open-source APFS writer
с resize, поэтому этот шаг выполняется на macOS или в другой проверенной среде
с записью APFS. Можно также передать `ramdisk.img4` из macOS-сборки для того же
устройства и IPSW: Windows-сборщик сам извлечёт из него raw APFS payload.

### Если собственного Mac нет

В репозитории есть ручной GitHub Actions workflow
`.github/workflows/build-23f84.yml`. Он использует облачный macOS runner только
для APFS-этапа и собирает готовый Windows bootchain для iPhone SE 2
(`iPhone12,8`, `d79ap`) на iOS 26.5.2 (`23F84`).

1. Сделайте fork репозитория на GitHub и отправьте в него Windows-порт.
2. Откройте `Actions` → `Build 23F84 ramdisk for Windows` → `Run workflow`.
3. После завершения скачайте artifact
   `ICH-d79ap-26.5.2-23F84-ramdisk` и распакуйте его на Windows.
4. После входа устройства в pwned DFU запустите:

```powershell
.\windows\ich.ps1 boot --bootchain "D:\ICH\d79ap-26.5.2-23F84-ramdisk" --with-fw
```

Workflow запускается только вручную, не получает секретов и не взаимодействует
с устройством. Для private fork расходуются включённые в тариф GitHub Actions
macOS minutes; условия публичных репозиториев зависят от текущих правил GitHub.

После подготовки повторите те же параметры прошивки:

```powershell
.\windows\ich.ps1 build --version <iOS-version> `
  --prepared-ramdisk ".\prepared-ramdisk\<prepared-image>.dmg"
```

Полезные варианты:

```powershell
.\windows\ich.ps1 build --list --product iPhone11,8 --board n841ap --cpid 0x8020
.\windows\ich.ps1 build --version <version> --no-fw
.\windows\ich.ps1 build --version <version> --kernel stock
.\windows\ich.ps1 build --url C:\IPSW\firmware.ipsw --version <version>
```

`--with-fw` включён по умолчанию. Повторяйте `--no-fw`, `--kernel` и остальные
опции на втором проходе, если использовали их на первом.

## Загрузка и SSH

Верните устройство в pwned DFU и выполните:

```powershell
.\windows\ich.ps1 status
.\windows\ich.ps1 boot
```

`boot` загрузит patched iBoot, дождётся USB Recovery, затем отправит SPTM/TXM,
SEP, coprocessor firmware, DeviceTree, trustcache, ramdisk и kernel в том же
порядке, что и `boot.sh`. Логотип на Windows по умолчанию пропускается; для
готового `bootchain\...\logo.img4` можно передать `--logo`.

После запуска ramdisk оставьте первое окно открытым и во втором запустите:

```powershell
.\windows\ich.ps1 iproxy 2222 22
```

В третьем окне:

```powershell
ssh root@localhost -p 2222
# password: alpine
mount_ich
```

Если `iproxy` не подключается к `127.0.0.1:27015`, установите Apple Devices или
iTunes и проверьте, что служба Apple Mobile Device Service запущена.

## Проверка порта без устройства

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s .\windows\tests -v
.\.venv\Scripts\python.exe -m compileall -q .\windows
```

Обнаружение Recovery и команда `getenv` проверены на реальном A13-устройстве.
Перед практическим использованием всё равно проверьте полный DFU upload и boot
на своём устройстве и своей версии IPSW.
