/* Phase 1b guest agent -- guest/init.c plus a `work` builtin.
 *
 * Why this exists: the stock guest agent only handles echo/cat, so a fork can't
 * be made to dirty memory or stay resident on demand. For a CoW-under-execution
 * proof we add one builtin:
 *
 *     work <mb> <spins>
 *        1. mmap <mb> MiB of ANONYMOUS memory and touch every page -> this
 *           dirties guest-physical pages, which forces host copy-on-write on the
 *           fork's MAP_PRIVATE snapshot mapping -> ~<mb> MiB of PRIVATE host RSS.
 *        2. busy-spin <spins> iterations to HOLD the fork resident. We must BUSY
 *           spin, not sleep: a sleeping guest HLTs, and the host's
 *           run_until_marker treats HLT-with-no-input as "done" and tears the
 *           fork down (kvm.rs). Busy spin keeps the vCPU running so the exec
 *           request (hence the fork) stays alive for the whole measurement.
 *
 * The retained allocation is intentionally never freed so pages stay resident
 * until the VM is dropped. Keep <mb> well under the snapshot's mem_mib or the
 * guest OOMs (no swap; killing PID1 panics the guest).
 *
 * Build (static, like `make guest`):
 *   musl-gcc -static -Os -march=x86-64 -mno-avx -mno-avx2 \
 *       -o guest/init_agentzip init_agentzip.c
 * Then: AGENT=$ZB_DIR/guest/init_agentzip bash 10_build_agent_rootfs.sh
 */
#define _GNU_SOURCE
#include <fcntl.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/mount.h>
#include <unistd.h>

#define SERIAL_DEV "/dev/ttyS0"
#define BUF_SIZE 65536

static int serial_fd;

static void serial_puts(const char *s) {
    int len = 0;
    while (s[len]) len++;
    write(serial_fd, s, len);
}

static int serial_read_line(char *buf, int size) {
    int i = 0;
    while (i < size - 1) {
        char c;
        int n = read(serial_fd, &c, 1);
        if (n <= 0) continue;
        if (c == '\n' || c == '\r') { if (i == 0) continue; buf[i] = 0; return i; }
        buf[i++] = c;
    }
    buf[i] = 0;
    return i;
}

/* kept so the dirtied pages stay resident for the whole hold */
static volatile char *g_hold;
static size_t g_hold_n;

static void eval_builtin(const char *cmd) {
    if (cmd[0] == 0) return;

    if (strncmp(cmd, "echo ", 5) == 0) { serial_puts(cmd + 5); serial_puts("\n"); return; }

    if (strncmp(cmd, "cat ", 4) == 0) {
        char buf[4096];
        int fd = open(cmd + 4, O_RDONLY);
        if (fd >= 0) { int n; while ((n = read(fd, buf, sizeof(buf)-1)) > 0) { buf[n]=0; serial_puts(buf);} close(fd);}
        else { serial_puts("error: cannot open "); serial_puts(cmd + 4); serial_puts("\n"); }
        return;
    }

    /* work <mb> <spins> */
    if (strncmp(cmd, "work ", 5) == 0) {
        char *end;
        long mb = strtol(cmd + 5, &end, 10);
        long spins = strtol(end, &end, 10);
        if (mb > 0) {
            size_t n = (size_t)mb * 1024 * 1024;
            char *buf = mmap(NULL, n, PROT_READ | PROT_WRITE,
                             MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
            if (buf != MAP_FAILED) {
                for (size_t i = 0; i < n; i += 4096) buf[i] = (char)(i >> 12);
                g_hold = buf; g_hold_n = n;   /* retain */
            } else {
                serial_puts("work: mmap failed\n");
            }
        }
        volatile unsigned long acc = 0;
        for (long i = 0; i < spins; i++) acc += (unsigned long)i;
        (void)acc;
        serial_puts("worked\n");
        return;
    }

    serial_puts("unknown: "); serial_puts(cmd); serial_puts("\n");
}

int main(void) {
    mount("proc", "/proc", "proc", 0, 0);
    mount("sysfs", "/sys", "sysfs", 0, 0);
    mount("devtmpfs", "/dev", "devtmpfs", 0, 0);

    serial_fd = open(SERIAL_DEV, O_RDWR | O_NOCTTY);
    if (serial_fd < 0) _exit(1);

    serial_puts("READY\n");

    char cmd[BUF_SIZE];
    while (1) {
        int len = serial_read_line(cmd, sizeof(cmd));
        if (len > 0) { eval_builtin(cmd); serial_puts("ZEROBOOT_DONE\n"); }
    }
}
