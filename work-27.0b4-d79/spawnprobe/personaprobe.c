// personaprobe - does persona 99 actually exist on this device?
//
// posix_spawn with persona 99 fails ESRCH ("no such process"), not EPERM.
// The attribute call itself succeeds, so the kernel rejects it at spawn time
// when it looks the persona up. That points at an empty/unpopulated persona
// table rather than a permission check, which would be a userland fix rather
// than a kernel patch.
//
// Uses the kpersona_* SPI via dlsym; it is private and not in the public SDK.

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <dlfcn.h>
#include <errno.h>
#include <sys/types.h>

// struct kpersona_info from XNU bsd/sys/persona.h; only the head is needed.
struct kpersona_info_head {
    unsigned int persona_info_version;
    uid_t        persona_id;
    int          persona_type;
    gid_t        persona_gid;
    unsigned int persona_ngroups;
    gid_t        persona_groups[16];
    uid_t        persona_gmuid;
    char         persona_name[257];
};

typedef int (*kpersona_info_t)(uid_t, void *);
typedef int (*kpersona_find_t)(const char *, uid_t, uid_t *, size_t *);
typedef int (*kpersona_get_t)(uid_t *);

static const char *ptype(int t) {
    switch (t) {
        case 1: return "GUEST";
        case 2: return "MANAGED";
        case 3: return "PRIV";
        case 4: return "SYSTEM";
        case 5: return "SYSTEM_PROXY";
        case 6: return "SYS_EXT";
        case 7: return "ENTERPRISE";
        default: return "?";
    }
}

int main(void) {
    kpersona_info_t kinfo = (kpersona_info_t)dlsym(RTLD_DEFAULT, "kpersona_info");
    kpersona_find_t kfind = (kpersona_find_t)dlsym(RTLD_DEFAULT, "kpersona_find");
    kpersona_get_t  kget  = (kpersona_get_t) dlsym(RTLD_DEFAULT, "kpersona_get");

    printf("uid=%d euid=%d\n", getuid(), geteuid());
    printf("SPI: kpersona_info=%p kpersona_find=%p kpersona_get=%p\n\n",
           (void *)kinfo, (void *)kfind, (void *)kget);
    if (!kinfo) { printf("kpersona_info unavailable, cannot enumerate\n"); return 1; }

    if (kget) {
        uid_t cur = 0;
        int rc = kget(&cur);
        printf("current persona: rc=%d id=%u\n\n", rc, cur);
    }

    printf("=== enumerating persona ids 0..120 ===\n");
    int found = 0;
    for (uid_t id = 0; id <= 120; id++) {
        struct kpersona_info_head info;
        memset(&info, 0, sizeof(info));
        info.persona_info_version = 1;
        if (kinfo(id, &info) != 0) continue;
        found++;
        printf("  id=%-4u type=%-12s gid=%-6u name=%s\n",
               info.persona_id, ptype(info.persona_type),
               info.persona_gid, info.persona_name);
    }
    printf("  total personas found: %d\n", found);

    printf("\n=== is persona 99 specifically present? ===\n");
    struct kpersona_info_head i99;
    memset(&i99, 0, sizeof(i99));
    i99.persona_info_version = 1;
    int rc = kinfo(99, &i99);
    if (rc == 0) printf("  YES: id=%u type=%s name=%s\n",
                        i99.persona_id, ptype(i99.persona_type), i99.persona_name);
    else         printf("  NO: kpersona_info(99) rc=%d errno=%d (%s)\n",
                        rc, errno, strerror(errno));
    return 0;
}
