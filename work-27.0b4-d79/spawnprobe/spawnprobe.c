// spawnprobe - where exactly does Sileo's spawnAsRoot die?
//
// Sileo's spawnAsRoot() does posix_spawn with persona 99 + uid/gid 0. On this
// device those calls fail silently (Sileo never checks the return), so mkdir
// and dpkg simply never run. This splits the path into three cases so the
// failure can be attributed:
//
//   PLAIN           ordinary posix_spawn, no persona
//   PERSONA99       persona selected, no uid/gid override
//   PERSONA99_ROOT  persona + uid 0 + gid 0, exactly what Sileo does
//
// posix_spawn returns the error number directly rather than setting errno,
// so the return value is what gets printed.
//
// Private SPI is resolved with dlsym to avoid fighting the public SDK linker.
// Must be run as mobile to model Sileo: su mobile -c .../spawnprobe

#include <stdio.h>
#include <unistd.h>
#include <spawn.h>
#include <sys/wait.h>
#include <dlfcn.h>
#include <string.h>

extern char **environ;

#define PERSONA_OVERRIDE 0x1

typedef int (*set_persona_t)(posix_spawnattr_t *, uid_t, unsigned int);
typedef int (*set_uid_t)(posix_spawnattr_t *, uid_t);
typedef int (*set_gid_t)(posix_spawnattr_t *, gid_t);

static set_persona_t set_persona;
static set_uid_t     set_persona_uid;
static set_gid_t     set_persona_gid;

static const char *kID = "/var/jb/usr/bin/id";

static void run_test(const char *name, int use_persona, int root_override) {
    posix_spawnattr_t attr;
    pid_t pid = -1;
    int rc, status;

    posix_spawnattr_init(&attr);

    if (use_persona) {
        rc = set_persona(&attr, 99, PERSONA_OVERRIDE);
        printf("[%s] set_persona(99) = %d (%s)\n", name, rc, rc ? strerror(rc) : "OK");
        if (rc != 0) goto out;

        if (root_override) {
            rc = set_persona_uid(&attr, 0);
            printf("[%s] set_uid(0) = %d (%s)\n", name, rc, rc ? strerror(rc) : "OK");
            if (rc != 0) goto out;
            rc = set_persona_gid(&attr, 0);
            printf("[%s] set_gid(0) = %d (%s)\n", name, rc, rc ? strerror(rc) : "OK");
            if (rc != 0) goto out;
        }
    }

    char *argv[] = { (char *)kID, NULL };
    rc = posix_spawn(&pid, kID, NULL, use_persona ? &attr : NULL, argv, environ);
    printf("[%s] posix_spawn = %d (%s), pid=%d\n",
           name, rc, rc ? strerror(rc) : "OK", pid);

    if (rc == 0) {
        waitpid(pid, &status, 0);
        if (WIFEXITED(status)) printf("[%s] child exit=%d\n", name, WEXITSTATUS(status));
        else                   printf("[%s] child status=0x%x\n", name, status);
    }

out:
    posix_spawnattr_destroy(&attr);
}

int main(void) {
    set_persona     = (set_persona_t)dlsym(RTLD_DEFAULT, "posix_spawnattr_set_persona_np");
    set_persona_uid = (set_uid_t)    dlsym(RTLD_DEFAULT, "posix_spawnattr_set_persona_uid_np");
    set_persona_gid = (set_gid_t)    dlsym(RTLD_DEFAULT, "posix_spawnattr_set_persona_gid_np");

    if (!set_persona || !set_persona_uid || !set_persona_gid) {
        printf("persona SPI missing: persona=%p uid=%p gid=%p\n",
               (void *)set_persona, (void *)set_persona_uid, (void *)set_persona_gid);
        return 1;
    }

    printf("probe uid=%d euid=%d gid=%d egid=%d\n\n",
           getuid(), geteuid(), getgid(), getegid());

    run_test("PLAIN", 0, 0);           puts("");
    run_test("PERSONA99", 1, 0);       puts("");
    run_test("PERSONA99_ROOT", 1, 1);
    return 0;
}
