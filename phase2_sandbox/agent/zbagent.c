/* zbagent -- PID 1 for ksm Phase 2 Zeroboot sandboxes.
 *
 * TEMPLATE BOOT (runs once, inside Firecracker, before the snapshot):
 *   Zeroboot forks have NO block device, so the whole root filesystem must be
 *   in guest RAM when the snapshot is taken. We:
 *     1. mount a tmpfs at /zbroot and copy every top-level entry of the ext4
 *        root into it (cp -a -x, from the image itself);
 *     2. pivot_root into the tmpfs and lazily unmount the disk;
 *     3. mount proc/sys/dev/dev/pts/dev/shm, quiet the kernel console, set the
 *        guest THP mode, optionally run a warm-up script;
 *     4. drop caches, put ttyS0 in raw mode and print ZEROBOOT_TEMPLATE_READY
 *        (zeroboot patch 0006 snapshots on that marker);
 *     5. block reading requests from the serial port. Every fork resumes here.
 *
 *   Optional /zb-config (KEY=VALUE lines, read from the image):
 *     TMPFS_SIZE=90%          size= option for the in-RAM root
 *     THP=madvise             guest /sys/kernel/mm/transparent_hugepage/enabled
 *     WARMUP=/zb-warmup.sh    script run once (as /bin/sh) before the snapshot
 *     WARMUP_TIMEOUT=600      seconds
 *     ENV_FILE=/zb-env        KEY=VALUE lines: the environment for every exec
 *                             (the docker image's Config.Env)
 *     WORKDIR=/testbed        default cwd for exec
 *
 * PROTOCOL (serial, host -> guest; one ASCII line per request):
 *   A <id> <b64>               append decoded bytes to the staging buffer
 *   R <id>                     clear the staging buffer
 *   X <id> <timeout_ms> <cap>  exec: staging = cwd '\0' command (sh -c),
 *                              stdout+stderr merged, at most <cap> bytes returned
 *   P <id> <mode_octal>        write file: staging = path '\0' data (mkdir -p)
 *   I <id> <seed> <epoch_ns>   per-sandbox init: mix seed into the kernel RNG,
 *                              set CLOCK_REALTIME
 *   Q <id>                     ping
 * guest -> host frames:
 *   "\x01ZB <id> O <len>\n" + <len> raw bytes      command output
 *   "\x01ZB <id> E <code> <flags>\n"                end of request
 *       code: exit status (128+sig if killed), or -errno for agent errors
 *       flags: bit0 output truncated at <cap>, bit1 timed out (code 124)
 * Lines are kept <= ~12 KB by the host (staging in 8 KB chunks) so the guest
 * tty flip buffers never overflow.
 *
 * Build (static, like zeroboot's `make guest`):
 *   musl-gcc -static -O2 -march=x86-64 -mno-avx -mno-avx2 -o zbagent zbagent.c
 */
#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
/* musl has no kernel headers: RNDADDENTROPY = _IOW('R', 0x03, int[2]) (stable ABI) */
#ifndef RNDADDENTROPY
#define RNDADDENTROPY 0x40085203
#endif
#include <poll.h>
#include <signal.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <termios.h>
#include <time.h>
#include <unistd.h>

#define SERIAL "/dev/ttyS0"
#define READY_MARKER "ZEROBOOT_TEMPLATE_READY"

static int ser = -1;

/* ---------- small helpers ---------- */

static void write_all(int fd, const void *buf, size_t n) {
    const char *p = buf;
    while (n > 0) {
        ssize_t w = write(fd, p, n);
        if (w < 0) { if (errno == EINTR || errno == EAGAIN) continue; return; }
        p += w; n -= (size_t)w;
    }
}

/* Boot-time diagnostics go to the console (and so to zeroboot's console.log). */
static void logmsg(const char *fmt, ...) {
    char b[1024];
    va_list ap; va_start(ap, fmt);
    int n = vsnprintf(b, sizeof b - 1, fmt, ap);
    va_end(ap);
    if (n < 0) return;
    if (n > (int)sizeof b - 2) n = sizeof b - 2;
    b[n++] = '\n';
    int fd = ser >= 0 ? ser : open("/dev/console", O_WRONLY | O_NOCTTY);
    if (fd >= 0) { write_all(fd, "zbagent: ", 9); write_all(fd, b, (size_t)n); if (fd != ser) close(fd); }
}

static void frame_out(const char *id, const char *data, size_t n) {
    char h[96];
    int hl = snprintf(h, sizeof h, "\x01ZB %s O %zu\n", id, n);
    write_all(ser, h, (size_t)hl);
    write_all(ser, data, n);
}

static void frame_end(const char *id, int code, int flags) {
    char h[96];
    int hl = snprintf(h, sizeof h, "\x01ZB %s E %d %d\n", id, code, flags);
    write_all(ser, h, (size_t)hl);
}

static int write_file(const char *path, const char *s) {
    int fd = open(path, O_WRONLY | O_TRUNC);
    if (fd < 0) return -1;
    write_all(fd, s, strlen(s));
    close(fd);
    return 0;
}

static int b64val(int c) {
    if (c >= 'A' && c <= 'Z') return c - 'A';
    if (c >= 'a' && c <= 'z') return c - 'a' + 26;
    if (c >= '0' && c <= '9') return c - '0' + 52;
    if (c == '+') return 62;
    if (c == '/') return 63;
    return -1;
}

/* Decode base64 `in` (stops at whitespace/NUL/'='), appending to *buf. */
static int b64_append(const char *in, char **buf, size_t *len, size_t *cap) {
    size_t inl = strcspn(in, " \n\r=");
    size_t need = *len + inl / 4 * 3 + 3;
    if (need > *cap) {
        size_t nc = *cap ? *cap : 65536;
        while (nc < need) nc *= 2;
        char *nb = realloc(*buf, nc);
        if (!nb) return -ENOMEM;
        *buf = nb; *cap = nc;
    }
    uint32_t acc = 0; int bits = 0;
    for (size_t i = 0; i < inl; i++) {
        int v = b64val((unsigned char)in[i]);
        if (v < 0) return -EINVAL;
        acc = (acc << 6) | (uint32_t)v; bits += 6;
        if (bits >= 8) { bits -= 8; (*buf)[(*len)++] = (char)((acc >> bits) & 0xff); }
    }
    (*buf)[*len] = 0;   /* keep staging NUL-terminated: the command is used as a C string */
    return 0;
}

static int mkdir_p(const char *path, mode_t mode) {   /* parents of path */
    char tmp[4096];
    if (strlen(path) >= sizeof tmp) return -ENAMETOOLONG;
    strcpy(tmp, path);
    for (char *p = tmp + 1; *p; p++) {
        if (*p == '/') {
            *p = 0;
            if (mkdir(tmp, mode) < 0 && errno != EEXIST) return -errno;
            *p = '/';
        }
    }
    return 0;
}

/* ---------- config + environment ---------- */

static char cfg_tmpfs_size[64] = "90%";
static char cfg_thp[32] = "madvise";
static char cfg_warmup[512] = "/zb-warmup.sh";
static int  cfg_warmup_timeout = 600;
static char cfg_env_file[512] = "/zb-env";
static char cfg_workdir[512] = "/";

static void load_config(const char *path) {
    FILE *f = fopen(path, "r");
    if (!f) return;
    char line[1024];
    while (fgets(line, sizeof line, f)) {
        line[strcspn(line, "\r\n")] = 0;
        char *eq = strchr(line, '=');
        if (!eq || line[0] == '#') continue;
        *eq = 0; const char *k = line, *v = eq + 1;
        if (!strcmp(k, "TMPFS_SIZE")) snprintf(cfg_tmpfs_size, sizeof cfg_tmpfs_size, "%s", v);
        else if (!strcmp(k, "THP")) snprintf(cfg_thp, sizeof cfg_thp, "%s", v);
        else if (!strcmp(k, "WARMUP")) snprintf(cfg_warmup, sizeof cfg_warmup, "%s", v);
        else if (!strcmp(k, "WARMUP_TIMEOUT")) cfg_warmup_timeout = atoi(v);
        else if (!strcmp(k, "ENV_FILE")) snprintf(cfg_env_file, sizeof cfg_env_file, "%s", v);
        else if (!strcmp(k, "WORKDIR")) snprintf(cfg_workdir, sizeof cfg_workdir, "%s", v);
    }
    fclose(f);
}

static char **exec_env;

static void load_env(void) {
    size_t n = 0, cap = 64;
    exec_env = calloc(cap, sizeof *exec_env);
    int have_path = 0, have_home = 0;
    FILE *f = fopen(cfg_env_file, "r");
    if (f) {
        char *line = NULL; size_t ll = 0;
        while (getline(&line, &ll, f) > 0) {
            line[strcspn(line, "\r\n")] = 0;
            if (!strchr(line, '=')) continue;
            if (!strncmp(line, "PATH=", 5)) have_path = 1;
            if (!strncmp(line, "HOME=", 5)) have_home = 1;
            if (n + 3 >= cap) { cap *= 2; exec_env = realloc(exec_env, cap * sizeof *exec_env); }
            exec_env[n++] = strdup(line);
        }
        free(line); fclose(f);
    }
    if (n + 3 >= cap) { cap += 4; exec_env = realloc(exec_env, cap * sizeof *exec_env); }
    if (!have_path) exec_env[n++] = strdup("PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin");
    if (!have_home) exec_env[n++] = strdup("HOME=/root");
    exec_env[n] = NULL;
}

/* Run argv to completion with output to the console; returns exit status. */
static int run_console(char *const argv[], int timeout_s) {
    pid_t pid = fork();
    if (pid < 0) return -1;
    if (pid == 0) {
        setsid();
        int c = open("/dev/console", O_RDWR | O_NOCTTY);
        if (c >= 0) { dup2(c, 0); dup2(c, 1); dup2(c, 2); if (c > 2) close(c); }
        execve(argv[0], argv, exec_env ? exec_env : (char *[]){ "PATH=/usr/bin:/bin", NULL });
        _exit(127);
    }
    int st = 0;
    for (int waited = 0; ; waited++) {
        pid_t r = waitpid(pid, &st, WNOHANG);
        if (r == pid) break;
        if (timeout_s > 0 && waited >= timeout_s * 10) { kill(-pid, SIGKILL); waitpid(pid, &st, 0); return -2; }
        usleep(100000);
    }
    return WIFEXITED(st) ? WEXITSTATUS(st) : 128 + WTERMSIG(st);
}

/* ---------- template boot: root filesystem into RAM ---------- */

static int root_to_ram(void) {
    mount("proc", "/proc", "proc", 0, 0);
    mkdir("/zbroot", 0755);
    char opts[96];
    snprintf(opts, sizeof opts, "size=%s,mode=0755", cfg_tmpfs_size);
    if (mount("tmpfs", "/zbroot", "tmpfs", 0, opts) < 0) { logmsg("mount tmpfs: %s", strerror(errno)); return -1; }

    DIR *d = opendir("/");
    if (!d) return -1;
    struct dirent *e;
    struct timespec t0, t1; clock_gettime(CLOCK_MONOTONIC, &t0);
    while ((e = readdir(d))) {
        const char *n = e->d_name;
        if (!strcmp(n, ".") || !strcmp(n, "..") || !strcmp(n, "proc") || !strcmp(n, "sys") ||
            !strcmp(n, "dev") || !strcmp(n, "zbroot") || !strcmp(n, "lost+found")) continue;
        char src[600];
        snprintf(src, sizeof src, "/%s", n);
        char *argv[] = { "/bin/cp", "-a", "-x", src, "/zbroot/", NULL };
        int rc = run_console(argv, 0);
        if (rc != 0) { logmsg("cp -a %s failed (%d)", src, rc); closedir(d); return -1; }
    }
    closedir(d);
    clock_gettime(CLOCK_MONOTONIC, &t1);
    logmsg("root copied to tmpfs in %.1fs", (t1.tv_sec - t0.tv_sec) + (t1.tv_nsec - t0.tv_nsec) / 1e9);

    umount2("/proc", MNT_DETACH);
    static const char *dirs[] = { "proc", "sys", "dev", "oldroot", "tmp", "run", NULL };
    for (int i = 0; dirs[i]; i++) {
        char p[64]; snprintf(p, sizeof p, "/zbroot/%s", dirs[i]); mkdir(p, 0755);
    }
    chmod("/zbroot/tmp", 01777);

    /* pivot into the tmpfs and drop the disk */
    mount(NULL, "/", NULL, MS_REC | MS_PRIVATE, NULL);
    if (chdir("/zbroot") < 0) return -1;
    if (syscall(SYS_pivot_root, ".", "oldroot") < 0) { logmsg("pivot_root: %s", strerror(errno)); return -1; }
    if (chdir("/") < 0) return -1;
    if (umount2("/oldroot", MNT_DETACH) < 0) logmsg("umount oldroot: %s", strerror(errno));
    rmdir("/oldroot");
    return 0;
}

static void mount_basics(void) {
    mount("proc", "/proc", "proc", 0, 0);
    mount("sysfs", "/sys", "sysfs", 0, 0);
    mount("devtmpfs", "/dev", "devtmpfs", 0, 0);
    mkdir("/dev/pts", 0755);
    mount("devpts", "/dev/pts", "devpts", 0, "ptmxmode=0666");
    mkdir("/dev/shm", 01777);
    mount("tmpfs", "/dev/shm", "tmpfs", 0, "mode=1777");
    mount("tmpfs", "/run", "tmpfs", 0, "mode=0755");
}

static int serial_open_raw(void) {
    int fd = open(SERIAL, O_RDWR | O_NOCTTY);
    if (fd < 0) return -1;
    struct termios t;
    if (tcgetattr(fd, &t) == 0) {
        cfmakeraw(&t);           /* no echo, no CR/LF mangling, byte-at-a-time */
        t.c_cc[VMIN] = 1; t.c_cc[VTIME] = 0;
        tcsetattr(fd, TCSANOW, &t);
    }
    return fd;
}

static void template_boot(void) {
    load_config("/zb-config");
    load_env();
    if (root_to_ram() < 0) {
        logmsg("FATAL: could not move root into RAM; halting");
        for (;;) pause();
    }
    mount_basics();
    sethostname("sandbox", 7);
    write_file("/proc/sys/kernel/printk", "1 4 1 7");   /* keep kernel noise off the protocol port */
    char thp[64]; snprintf(thp, sizeof thp, "%s", cfg_thp);
    if (write_file("/sys/kernel/mm/transparent_hugepage/enabled", thp) < 0)
        logmsg("could not set guest THP=%s", thp);

    struct stat st;
    if (cfg_warmup[0] && stat(cfg_warmup, &st) == 0) {
        logmsg("warm-up: %s", cfg_warmup);
        char *argv[] = { "/bin/sh", cfg_warmup, NULL };
        int rc = run_console(argv, cfg_warmup_timeout);
        logmsg("warm-up exit %d", rc);
    }
    sync();
    write_file("/proc/sys/vm/drop_caches", "3");
}

/* ---------- requests ---------- */

static char *stage; static size_t stage_len, stage_cap;

static void do_exec(const char *id, long timeout_ms, long cap) {
    const char *cwd = stage_len ? stage : "";
    size_t cwdl = strnlen(cwd, stage_len);
    if (cwdl >= stage_len) { frame_end(id, -EINVAL, 0); return; }
    const char *cmd = stage + cwdl + 1;
    if (!*cwd) cwd = cfg_workdir;

    int pfd[2];
    if (pipe2(pfd, O_CLOEXEC) < 0) { frame_end(id, -errno, 0); return; }
    pid_t pid = fork();
    if (pid < 0) { frame_end(id, -errno, 0); close(pfd[0]); close(pfd[1]); return; }
    if (pid == 0) {
        setsid();
        int dn = open("/dev/null", O_RDONLY);
        if (dn >= 0) dup2(dn, 0);
        dup2(pfd[1], 1); dup2(pfd[1], 2);
        if (chdir(cwd) < 0) {
            dprintf(2, "zbagent: cd %s: %s\n", cwd, strerror(errno));
            _exit(126);
        }
        char *argv[] = { "/bin/sh", "-c", (char *)cmd, NULL };
        execve("/bin/sh", argv, exec_env);
        dprintf(2, "zbagent: exec /bin/sh: %s\n", strerror(errno));
        _exit(127);
    }
    close(pfd[1]);

    struct timespec t0; clock_gettime(CLOCK_MONOTONIC, &t0);
    char buf[16384];
    long sent = 0; int flags = 0, st = 0, exited = 0, idle_after_exit = 0;
    for (;;) {
        struct timespec now; clock_gettime(CLOCK_MONOTONIC, &now);
        long el = (now.tv_sec - t0.tv_sec) * 1000 + (now.tv_nsec - t0.tv_nsec) / 1000000;
        if (!exited && timeout_ms > 0 && el >= timeout_ms) {
            kill(-pid, SIGKILL);
            waitpid(pid, &st, 0);
            exited = 1; flags |= 2;
        }
        struct pollfd p = { pfd[0], POLLIN, 0 };
        int r = poll(&p, 1, exited ? 50 : 100);
        if (r > 0) {
            ssize_t n = read(pfd[0], buf, sizeof buf);
            if (n == 0) break;                       /* EOF: every writer closed */
            if (n < 0) { if (errno == EINTR) continue; break; }
            long room = cap - sent;
            if (room > 0) {
                size_t k = (size_t)(n < room ? n : room);
                frame_out(id, buf, k); sent += (long)k;
            }
            if ((long)n > room) flags |= 1;          /* past <cap>: keep draining, report truncation */
            idle_after_exit = 0;
        } else if (exited && ++idle_after_exit >= 4) {
            break;   /* child gone; a background process holds the pipe -- stop like docker exec would */
        }
        if (!exited && waitpid(pid, &st, WNOHANG) == pid) exited = 1;
    }
    close(pfd[0]);
    if (!exited) waitpid(pid, &st, 0);
    int code = (flags & 2) ? 124 : WIFEXITED(st) ? WEXITSTATUS(st) : 128 + WTERMSIG(st);
    frame_end(id, code, flags);
}

static void do_put(const char *id, const char *mode_s) {
    size_t pl = strnlen(stage, stage_len);
    if (!stage_len || pl >= stage_len) { frame_end(id, -EINVAL, 0); return; }
    const char *path = stage;
    const char *data = stage + pl + 1;
    size_t dl = stage_len - pl - 1;
    mode_t mode = (mode_t)strtol(mode_s, NULL, 8);
    int rc = mkdir_p(path, 0755);
    if (rc < 0) { frame_end(id, rc, 0); return; }
    int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, mode ? mode : 0644);
    if (fd < 0) { frame_end(id, -errno, 0); return; }
    write_all(fd, data, dl);
    if (mode) fchmod(fd, mode);
    close(fd);
    frame_end(id, 0, 0);
}

static void do_init(const char *id, unsigned long long seed, long long epoch_ns) {
    /* splitmix64 -> 32 bytes of seed-derived entropy, credited to the pool */
    struct { int count; int size; uint32_t buf[8]; } ent = { 256, 32, {0} };
    uint64_t x = seed;
    for (int i = 0; i < 8; i += 2) {
        uint64_t z = (x += 0x9E3779B97F4A7C15ULL);
        z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ULL;
        z = (z ^ (z >> 27)) * 0x94D049BB133111EBULL;
        z ^= z >> 31;
        ent.buf[i] = (uint32_t)z; ent.buf[i + 1] = (uint32_t)(z >> 32);
    }
    int fd = open("/dev/urandom", O_WRONLY);
    int rc = 0;
    if (fd >= 0) {
        if (ioctl(fd, RNDADDENTROPY, &ent) < 0) rc = -errno;
        close(fd);
    }
    if (epoch_ns > 0) {
        struct timespec ts = { (time_t)(epoch_ns / 1000000000LL), (long)(epoch_ns % 1000000000LL) };
        if (clock_settime(CLOCK_REALTIME, &ts) < 0 && !rc) rc = -errno;
    }
    frame_end(id, rc, 0);
}

static void handle(char *line) {
    char op = line[0];
    if (!op || line[1] != ' ') return;
    char *save = NULL;
    strtok_r(line, " ", &save);                     /* op */
    char *id = strtok_r(NULL, " ", &save);
    if (!id) return;
    switch (op) {
    case 'A': {
        char *b = strtok_r(NULL, " ", &save);
        int rc = b ? b64_append(b, &stage, &stage_len, &stage_cap) : -EINVAL;
        frame_end(id, rc, 0);
        break;
    }
    case 'R': stage_len = 0; if (stage) stage[0] = 0; frame_end(id, 0, 0); break;
    case 'Q': frame_end(id, 0, 0); break;
    case 'X': {
        char *t = strtok_r(NULL, " ", &save), *c = strtok_r(NULL, " ", &save);
        do_exec(id, t ? atol(t) : 0, c ? atol(c) : (1L << 20));
        stage_len = 0;
        break;
    }
    case 'P': {
        char *m = strtok_r(NULL, " ", &save);
        do_put(id, m ? m : "0");
        stage_len = 0;
        break;
    }
    case 'I': {
        char *sd = strtok_r(NULL, " ", &save), *ep = strtok_r(NULL, " ", &save);
        do_init(id, sd ? strtoull(sd, NULL, 10) : 0, ep ? atoll(ep) : 0);
        break;
    }
    default: frame_end(id, -ENOSYS, 0);
    }
}

static void serve(void) {
    size_t cap = 1 << 16, len = 0;
    char *buf = malloc(cap);
    for (;;) {
        while (waitpid(-1, NULL, WNOHANG) > 0) {}    /* PID 1 reaps orphans */
        if (len + 4096 > cap) { cap *= 2; buf = realloc(buf, cap); }
        ssize_t n = read(ser, buf + len, cap - len - 1);
        if (n <= 0) { if (n < 0 && errno != EINTR && errno != EAGAIN) usleep(1000); continue; }
        len += (size_t)n;
        size_t start = 0;
        for (size_t i = 0; i < len; i++) {
            if (buf[i] == '\n' || buf[i] == '\r') {
                buf[i] = 0;
                if (i > start) handle(buf + start);
                start = i + 1;
            }
        }
        memmove(buf, buf + start, len - start);
        len -= start;
    }
}

int main(void) {
    if (getpid() == 1) template_boot();
    else { load_config("/zb-config"); load_env(); }   /* manual testing outside a VM */
    ser = serial_open_raw();
    if (ser < 0) { logmsg("cannot open %s", SERIAL); for (;;) pause(); }
    write_all(ser, READY_MARKER "\n", sizeof READY_MARKER);
    serve();
}
