// photodiag - find out WHY the photo library will not load.
//
// Camera, Photos and Settings all abort with the same stack:
//
//   PhotoLibraryServices  -[PLPhotoLibrary loadDatabaseWithOptions:error:]
//   PhotoLibraryServices  +[PLPhotoLibrary newPhotoLibraryWithName:loadedFromBundle:options:error:]
//   objc_exception_rethrow -> abort()
//
// The crash report discards the exception's reason, which is exactly the piece
// we need. This calls the same API inside @try/@catch and prints the exception
// name, reason and userInfo, plus any NSError handed back.
//
// Everything checked so far is healthy: assetsd is present at the exact IPSW
// size and running, the framework's 102 resources including every photos-*.mom
// schema match the IPSW, /var/mobile/Media/PhotoData exists and is writable by
// mobile, and there is 41 GB free. So the ingredients are there and creation
// still fails. This tool is meant to say why instead of guessing.
//
// Build: ./build.sh     Run as mobile, since that is who owns the library:
//   su mobile -c /var/jb/usr/bin/photodiag

#import <Foundation/Foundation.h>
#import <dlfcn.h>
#import <objc/message.h>
#import <objc/runtime.h>

static const char *kPLS =
    "/System/Library/PrivateFrameworks/PhotoLibraryServices.framework/PhotoLibraryServices";

/// Print an NSError and its whole underlying chain, which is where Core Data
/// hides the useful detail.
static void dumpError(NSError *err, int depth) {
    if (err == nil) return;
    NSString *pad = [@"" stringByPaddingToLength:depth * 2 withString:@" " startingAtIndex:0];
    fprintf(stderr, "%s  domain : %s\n", pad.UTF8String, err.domain.UTF8String);
    fprintf(stderr, "%s  code   : %ld\n", pad.UTF8String, (long)err.code);
    fprintf(stderr, "%s  desc   : %s\n", pad.UTF8String, err.localizedDescription.UTF8String);
    for (NSString *k in err.userInfo) {
        id v = err.userInfo[k];
        if ([v isKindOfClass:[NSError class]]) {
            fprintf(stderr, "%s  %s ->\n", pad.UTF8String, k.UTF8String);
            dumpError((NSError *)v, depth + 1);
        } else {
            NSString *s = [NSString stringWithFormat:@"%@", v];
            if (s.length > 400) s = [s substringToIndex:400];
            fprintf(stderr, "%s  %s : %s\n", pad.UTF8String, k.UTF8String, s.UTF8String);
        }
    }
}

/// Invoke +[PLPhotoLibrary newPhotoLibraryWithName:loadedFromBundle:options:error:]
/// via NSInvocation, so a signature change on a future build fails loudly
/// rather than corrupting the stack.
static id newPhotoLibrary(Class cls, NSError **outErr) {
    SEL sel = NSSelectorFromString(
        @"newPhotoLibraryWithName:loadedFromBundle:options:error:");
    NSMethodSignature *sig = [cls methodSignatureForSelector:sel];
    if (sig == nil) {
        fprintf(stderr, "  selector not found: newPhotoLibraryWithName:...\n");
        return nil;
    }

    NSInvocation *inv = [NSInvocation invocationWithMethodSignature:sig];
    inv.target = cls;
    inv.selector = sel;

    NSString *name = nil;         // nil = the default system library
    BOOL fromBundle = NO;
    id options = nil;
    [inv setArgument:&name       atIndex:2];
    [inv setArgument:&fromBundle atIndex:3];
    [inv setArgument:&options    atIndex:4];
    [inv setArgument:&outErr     atIndex:5];

    [inv invoke];

    id result = nil;
    if (sig.methodReturnLength > 0)
        [inv getReturnValue:&result];
    return result;
}

/// Reproduce the path the real apps take. Their stack is
///   Photos  newPhotoLibrary
///   Photos  -[PHPhotoLibrary initWithPhotoLibraryBundle:type:]_block_invoke_3
///   Photos  -[PHPhotoLibrary mainQueuePhotoLibrary]
/// so going in via PHPhotoLibrary exercises the same code that throws, rather
/// than the lower-level PLPhotoLibrary entry point which fails earlier and
/// differently (error 45001, returns nil instead of throwing).
static void probePHPhotoLibrary(void) {
    printf("\n=== probe 2: PHPhotoLibrary (the route Camera/Photos/Settings use) ===\n");
    if (dlopen("/System/Library/Frameworks/Photos.framework/Photos", RTLD_NOW) == NULL) {
        fprintf(stderr, "  dlopen Photos failed: %s\n", dlerror());
        return;
    }
    Class ph = NSClassFromString(@"PHPhotoLibrary");
    if (ph == Nil) { fprintf(stderr, "  PHPhotoLibrary not found\n"); return; }

    // The class method succeeds because construction is lazy; the database is
    // only loaded when the library is actually demanded, which is where the
    // apps die. So get the shared instance first, then poke the INSTANCE
    // methods from the crash stack.
    id shared = nil;
    @try {
        shared = ((id (*)(id, SEL))objc_msgSend)(ph, NSSelectorFromString(@"sharedPhotoLibrary"));
        printf("  +sharedPhotoLibrary -> %s\n",
               shared ? [NSString stringWithFormat:@"%@", shared].UTF8String : "nil");
    } @catch (NSException *ex) {
        fprintf(stderr, "  +sharedPhotoLibrary THREW: %s / %s\n",
                ex.name.UTF8String, ex.reason.UTF8String);
        return;
    }
    if (shared == nil) return;

    for (NSString *selName in @[@"mainQueuePhotoLibrary",
                                @"photoLibraryForCurrentQueueQoS",
                                @"photoLibrary"]) {
        SEL sel = NSSelectorFromString(selName);
        if (![shared respondsToSelector:sel]) {
            printf("  -%s: not available\n", selName.UTF8String);
            continue;
        }
        @try {
            id r = ((id (*)(id, SEL))objc_msgSend)(shared, sel);
            printf("  -%s -> %s\n", selName.UTF8String,
                   r ? [NSString stringWithFormat:@"%@", r].UTF8String : "nil");
        } @catch (NSException *ex) {
            // This is the exception Camera/Photos/Settings die on.
            fprintf(stderr, "  -%s THREW  <<<< this is the app crash\n", selName.UTF8String);
            fprintf(stderr, "    name   : %s\n", ex.name.UTF8String);
            fprintf(stderr, "    reason : %s\n", ex.reason.UTF8String);
            for (id k in ex.userInfo) {
                NSString *s = [NSString stringWithFormat:@"%@", ex.userInfo[k]];
                if (s.length > 900) s = [s substringToIndex:900];
                fprintf(stderr, "    %s : %s\n",
                        [NSString stringWithFormat:@"%@", k].UTF8String, s.UTF8String);
            }
            return;
        }
    }
}


/// Probe the library BUNDLE, which is the argument that
/// initWithName:libraryBundle:options: is failing on. If the bundle cannot be
/// built for the system library path, that is the real root cause and
/// everything above it is a symptom.
static void probeBundle(void) {
    printf("\n=== probe 3: PLPhotoLibraryBundle (the failing argument) ===\n");
    Class b = NSClassFromString(@"PLPhotoLibraryBundle");
    if (b == Nil) { fprintf(stderr, "  PLPhotoLibraryBundle not found\n"); return; }

    // class-level constructors that need no arguments
    for (NSString *sn in @[@"systemLibraryBundle", @"systemPhotoLibraryBundle",
                           @"defaultLibraryBundle", @"systemLibraryURL",
                           @"systemLibraryPath"]) {
        SEL sel = NSSelectorFromString(sn);
        if (![b respondsToSelector:sel]) { printf("  +%s: n/a\n", sn.UTF8String); continue; }
        @try {
            id r = ((id (*)(id, SEL))objc_msgSend)(b, sel);
            printf("  +%s -> %s\n", sn.UTF8String,
                   r ? [NSString stringWithFormat:@"%@", r].UTF8String : "nil");
        } @catch (NSException *ex) {
            fprintf(stderr, "  +%s THREW: %s / %s\n", sn.UTF8String,
                    ex.name.UTF8String, ex.reason.UTF8String);
        }
    }

    // build one explicitly for the on-disk library path
    NSString *path = @"/var/mobile/Media/PhotoData";
    for (NSString *sn in @[@"bundleWithPath:", @"photoLibraryBundleWithPath:",
                           @"bundleWithURL:"]) {
        SEL sel = NSSelectorFromString(sn);
        if (![b respondsToSelector:sel]) { printf("  +%s: n/a\n", sn.UTF8String); continue; }
        id arg = [sn hasSuffix:@"URL:"] ? (id)[NSURL fileURLWithPath:path] : (id)path;
        @try {
            id r = ((id (*)(id, SEL, id))objc_msgSend)(b, sel, arg);
            printf("  +%s(%s) -> %s\n", sn.UTF8String, path.UTF8String,
                   r ? [NSString stringWithFormat:@"%@", r].UTF8String : "nil");
        } @catch (NSException *ex) {
            fprintf(stderr, "  +%s THREW: %s / %s\n", sn.UTF8String,
                    ex.name.UTF8String, ex.reason.UTF8String);
        }
    }
}


/// List what PLPhotoLibraryBundle actually exposes, instead of guessing
/// selector names. Class methods live on the metaclass.
static void dumpMethods(const char *clsName) {
    Class c = NSClassFromString([NSString stringWithUTF8String:clsName]);
    if (c == Nil) { printf("  %s: class not found\n", clsName); return; }
    printf("\n  --- %s class methods ---\n", clsName);
    unsigned n = 0;
    Method *m = class_copyMethodList(object_getClass(c), &n);
    for (unsigned i = 0; i < n; i++) {
        const char *nm = sel_getName(method_getName(m[i]));
        if (strstr(nm, "ibrary") || strstr(nm, "undle") || strstr(nm, "ystem") ||
            strstr(nm, "reate") || strstr(nm, "nit") || strstr(nm, "ath") || strstr(nm, "URL"))
            printf("    +%s\n", nm);
    }
    free(m);
}


/// Use the REAL selectors, discovered by enumerating the class rather than
/// guessing. +systemPhotoLibraryIsObtainable is the framework's own answer to
/// "can the system library be opened at all".
static void probeReal(void) {
    printf("\n=== probe 4: the actual PLPhotoLibrary API ===\n");
    Class c = NSClassFromString(@"PLPhotoLibrary");
    if (c == Nil) return;

    SEL ob = NSSelectorFromString(@"systemPhotoLibraryIsObtainable");
    if ([c respondsToSelector:ob]) {
        @try {
            BOOL r = ((BOOL (*)(id, SEL))objc_msgSend)(c, ob);
            printf("  +systemPhotoLibraryIsObtainable -> %s\n", r ? "YES" : "NO");
        } @catch (NSException *ex) {
            fprintf(stderr, "  +systemPhotoLibraryIsObtainable THREW: %s / %s\n",
                    ex.name.UTF8String, ex.reason.UTF8String);
        }
    }

    for (NSString *sn in @[@"systemPhotoLibrary", @"_internalSystemPhotoLibrary",
                           @"systemMainQueuePhotoLibrary", @"cameraPhotoLibrary"]) {
        SEL sel = NSSelectorFromString(sn);
        if (![c respondsToSelector:sel]) { printf("  +%s: n/a\n", sn.UTF8String); continue; }
        @try {
            id r = ((id (*)(id, SEL))objc_msgSend)(c, sel);
            printf("  +%s -> %s\n", sn.UTF8String,
                   r ? [NSString stringWithFormat:@"%@", r].UTF8String : "nil");
        } @catch (NSException *ex) {
            fprintf(stderr, "  +%s THREW  <<<< the app crash\n", sn.UTF8String);
            fprintf(stderr, "    name   : %s\n", ex.name.UTF8String);
            fprintf(stderr, "    reason : %s\n", ex.reason.UTF8String);
        }
    }

    // the URL variant, pointed explicitly at the on-disk library
    SEL u = NSSelectorFromString(@"newPhotoLibraryWithName:loadedFromURL:options:error:");
    NSMethodSignature *sig = [c methodSignatureForSelector:u];
    if (sig) {
        NSInvocation *inv = [NSInvocation invocationWithMethodSignature:sig];
        inv.target = c; inv.selector = u;
        NSString *name = nil;
        NSURL *url = [NSURL fileURLWithPath:@"/var/mobile/Media/PhotoData"];
        id opts = nil; NSError *e = nil; NSError **ep = &e;
        [inv setArgument:&name atIndex:2];
        [inv setArgument:&url  atIndex:3];
        [inv setArgument:&opts atIndex:4];
        [inv setArgument:&ep   atIndex:5];
        @try {
            [inv invoke];
            id r = nil; [inv getReturnValue:&r];
            printf("  +newPhotoLibraryWithName:loadedFromURL: -> %s\n",
                   r ? [NSString stringWithFormat:@"%@", r].UTF8String : "nil");
            if (e) dumpError(e, 1);
        } @catch (NSException *ex) {
            fprintf(stderr, "  loadedFromURL THREW: %s / %s\n",
                    ex.name.UTF8String, ex.reason.UTF8String);
        }
    }
}

int main(void) {
    @autoreleasepool {
        printf("uid=%d euid=%d\n", (int)getuid(), (int)geteuid());

        if (dlopen(kPLS, RTLD_NOW) == NULL) {
            fprintf(stderr, "dlopen PhotoLibraryServices failed: %s\n", dlerror());
            return 2;
        }

        Class cls = NSClassFromString(@"PLPhotoLibrary");
        if (cls == Nil) {
            fprintf(stderr, "PLPhotoLibrary class not found after dlopen\n");
            return 2;
        }
        printf("PLPhotoLibrary: %p\n\n", (void *)cls);

        NSError *err = nil;
        id lib = nil;

        @try {
            lib = newPhotoLibrary(cls, &err);
        } @catch (NSException *ex) {
            // This is the exception the crash report throws away.
            fprintf(stderr, "EXCEPTION\n");
            fprintf(stderr, "  name   : %s\n", ex.name.UTF8String);
            fprintf(stderr, "  reason : %s\n", ex.reason.UTF8String);
            if (ex.userInfo.count) {
                for (id k in ex.userInfo) {
                    NSString *s = [NSString stringWithFormat:@"%@", ex.userInfo[k]];
                    if (s.length > 600) s = [s substringToIndex:600];
                    fprintf(stderr, "  %s : %s\n",
                            [NSString stringWithFormat:@"%@", k].UTF8String, s.UTF8String);
                }
            }
            if (err) { fprintf(stderr, "ALSO SET AN ERROR:\n"); dumpError(err, 1); }
            probePHPhotoLibrary();
            probeBundle();
            dumpMethods("PLPhotoLibraryBundle");
            probeReal();
            return 1;
        }

        if (lib == nil) {
            fprintf(stderr, "returned nil WITHOUT throwing\n");
            if (err) dumpError(err, 1); else fprintf(stderr, "  and no NSError was set\n");
            probePHPhotoLibrary();
            probeBundle();
            dumpMethods("PLPhotoLibraryBundle");
            probeReal();
            return 1;
        }

        printf("SUCCESS: photo library loaded: %s\n",
               [NSString stringWithFormat:@"%@", lib].UTF8String);
        return 0;
    }
}
