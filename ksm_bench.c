// ksm_bench.c - one benchmark instance for the KSM cross-node penalty experiment.
//
// Two copies run at once (A pinned to node 0, B to node 1). They build
// byte-identical buffers so KSM can merge them, then time read (or
// read/write) passes over the buffer once the merge has finished.
//
// Driven by run_bench.py over stdin/stdout, one command per line:
//   fill                                 allocate + fill the buffer, mark it MADV_MERGEABLE
//   stat                                 count buffer pages resident on each NUMA node
//   run <chase|seq> <seconds> <write_frac>
//   quit
// Every reply is a single line: "OK key=value ..." or "ERR message".

#define _GNU_SOURCE
#include <errno.h>
#include <getopt.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

#define PAGE_SIZE      4096UL
#define LINE_SIZE      64UL
#define LINES_PER_PAGE (PAGE_SIZE / LINE_SIZE)
#define MAX_NODES      64
#define STAT_BATCH     16384
#define CHASE_CHUNK    (1UL << 18)   // loads between clock checks
#define SEQ_CHUNK      (1UL << 20)   // lines (64 MiB) between clock checks

// One cache line. Only `next` is read by the pointer chase; the padding
// carries the per-instance salt (unshared pages) and the write target.
typedef struct {
    uint64_t next;     // index of the next line in the chase cycle
    uint64_t salt;     // nonzero only on pages meant to differ between A and B
    uint64_t scratch;  // written by runs with write_frac > 0
    uint64_t pad[5];
} line_t;

_Static_assert(sizeof(line_t) == LINE_SIZE, "line_t must be one cache line");

static line_t *buf;
static size_t size_bytes, nlines, npages, nshared;
static uint64_t seed = 1;
static unsigned id;               // 0 = A, 1 = B
static double shared_frac = 1.0;
static int do_madvise = 1;
static uint64_t chase_pos, seq_pos;
static volatile uint64_t sink;    // keeps the loops from being optimized away

static double now(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec * 1e-9;
}

static uint64_t splitmix64(uint64_t *s)
{
    uint64_t z = (*s += 0x9e3779b97f4a7c15ULL);
    z = (z ^ (z >> 30)) * 0xbf58476d1ce4e5b9ULL;
    z = (z ^ (z >> 27)) * 0x94d049bb133111ebULL;
    return z ^ (z >> 31);
}

static uint64_t mix64(uint64_t x)
{
    return splitmix64(&x);
}

// Same answer in A and B: depends only on the shared seed and page index.
static int page_is_shared(size_t p)
{
    uint64_t h = mix64(seed ^ 0x5eed5eed00000000ULL ^ p);
    return (double)(h >> 11) / (double)(1ULL << 53) < shared_frac;
}

static long read_long_file(const char *path)
{
    FILE *f = fopen(path, "r");
    long v = -1;
    if (!f)
        return -1;
    if (fscanf(f, "%ld", &v) != 1)
        v = -1;
    fclose(f);
    return v;
}

static void reply_err(const char *fmt, const char *arg)
{
    printf("ERR ");
    printf(fmt, arg);
    printf("\n");
}

static void cmd_fill(void)
{
    if (buf) {
        reply_err("%s", "already filled");
        return;
    }
    double t0 = now();
    buf = mmap(NULL, size_bytes, PROT_READ | PROT_WRITE,
               MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (buf == MAP_FAILED) {
        buf = NULL;
        reply_err("mmap: %s", strerror(errno));
        return;
    }
    // 4 KiB pages in every configuration: KSM only merges base pages, and
    // keeping the KSM-off baseline on the same page size leaves physical
    // placement as the only thing that differs between configurations.
    madvise(buf, size_bytes, MADV_NOHUGEPAGE);

    // First touch allocates every page under the numactl memory policy.
    for (size_t i = 0; i < nlines; i++)
        buf[i].next = i;

    // Sattolo's shuffle: a single random cycle through every line, so the
    // chase visits the whole buffer and the prefetcher can't follow it.
    // The seed is shared, so A and B build the identical cycle.
    uint64_t s = seed;
    for (size_t i = nlines - 1; i > 0; i--) {
        size_t j = splitmix64(&s) % i;
        uint64_t t = buf[i].next;
        buf[i].next = buf[j].next;
        buf[j].next = t;
    }

    nshared = 0;
    for (size_t p = 0; p < npages; p++) {
        if (page_is_shared(p))
            nshared++;
        else
            buf[p * LINES_PER_PAGE].salt = mix64(((uint64_t)id + 1) << 48 ^ p) | 1;
    }

    if (do_madvise && madvise(buf, size_bytes, MADV_MERGEABLE)) {
        reply_err("madvise(MADV_MERGEABLE): %s", strerror(errno));
        return;
    }

    unsigned cpu = 0, node = 0;
    syscall(SYS_getcpu, &cpu, &node, NULL);
    printf("OK secs=%.3f pages=%zu shared_pages=%zu cpu=%u node=%u\n",
           now() - t0, npages, nshared, cpu, node);
}

// Ask the kernel which node backs each page (move_pages with nodes == NULL
// only queries, it moves nothing). After a cross-node merge, the instance
// that lost its copies sees them resident on the other node.
static void cmd_stat(void)
{
    static void *pages[STAT_BATCH];
    static int status[STAT_BATCH];
    long counts[MAX_NODES] = {0};
    long absent = 0, other = 0;
    int max_node = 1;

    if (!buf) {
        reply_err("%s", "not filled");
        return;
    }
    for (size_t p = 0; p < npages; p += STAT_BATCH) {
        size_t n = npages - p < STAT_BATCH ? npages - p : STAT_BATCH;
        for (size_t k = 0; k < n; k++)
            pages[k] = (char *)buf + (p + k) * PAGE_SIZE;
        if (syscall(SYS_move_pages, 0, n, pages, NULL, status, 0) < 0) {
            reply_err("move_pages: %s", strerror(errno));
            return;
        }
        for (size_t k = 0; k < n; k++) {
            int st = status[k];
            if (st >= 0 && st < MAX_NODES) {
                counts[st]++;
                if (st > max_node)
                    max_node = st;
            } else if (st == -ENOENT) {
                absent++;
            } else {
                other++;
            }
        }
    }
    printf("OK pages=%zu", npages);
    for (int n = 0; n <= max_node; n++)
        printf(" n%d=%ld", n, counts[n]);
    printf(" absent=%ld err=%ld merging=%ld\n", absent, other,
           read_long_file("/proc/self/ksm_merging_pages"));
}

static uint64_t chase_ro(uint64_t steps)
{
    uint64_t c = chase_pos;
    for (uint64_t k = 0; k < steps; k++)
        c = buf[c].next;
    chase_pos = c;
    return 0;
}

// A store to a merged page takes a copy-on-write fault that gives this
// process a private copy, allocated under its own memory policy.
static uint64_t chase_rw(uint64_t steps, uint32_t thresh, uint64_t *rng, uint64_t tag)
{
    uint64_t c = chase_pos, writes = 0, r = *rng;
    for (uint64_t k = 0; k < steps; k++) {
        c = buf[c].next;
        r = r * 6364136223846793005ULL + 1442695040888963407ULL;
        if ((uint32_t)(r >> 32) < thresh) {
            buf[c].scratch = tag ^ k;
            writes++;
        }
    }
    chase_pos = c;
    *rng = r;
    return writes;
}

static uint64_t seq_ro(size_t start, size_t n)
{
    const uint64_t *w = (const uint64_t *)(buf + start);
    uint64_t sum = 0;
    for (size_t i = 0; i < n * (LINE_SIZE / 8); i++)
        sum += w[i];
    sink += sum;
    return 0;
}

static uint64_t seq_rw(size_t start, size_t n, uint32_t thresh, uint64_t *rng, uint64_t tag)
{
    uint64_t sum = 0, writes = 0, r = *rng;
    for (size_t i = start; i < start + n; i++) {
        const uint64_t *w = (const uint64_t *)(buf + i);
        for (size_t j = 0; j < LINE_SIZE / 8; j++)
            sum += w[j];
        r = r * 6364136223846793005ULL + 1442695040888963407ULL;
        if ((uint32_t)(r >> 32) < thresh) {
            buf[i].scratch = tag ^ i;
            writes++;
        }
    }
    sink += sum;
    *rng = r;
    return writes;
}

static void cmd_run(const char *pattern, double secs, double wfrac)
{
    int chase = !strcmp(pattern, "chase");
    if (!buf) {
        reply_err("%s", "not filled");
        return;
    }
    if (!chase && strcmp(pattern, "seq")) {
        reply_err("unknown pattern %s", pattern);
        return;
    }
    uint32_t thresh = wfrac <= 0 ? 0 : wfrac >= 1 ? UINT32_MAX : (uint32_t)(wfrac * 4294967296.0);
    uint64_t rng = mix64(seed ^ ((uint64_t)id << 32) ^ 0xabcdefULL);
    // Instance-specific values, so pages A and B both write never become
    // identical again and get re-merged.
    uint64_t tag = ((uint64_t)id + 1) << 56;
    uint64_t ops = 0, writes = 0;
    double t0 = now(), t;

    do {
        if (chase) {
            writes += thresh ? chase_rw(CHASE_CHUNK, thresh, &rng, tag) : chase_ro(CHASE_CHUNK);
            ops += CHASE_CHUNK;
        } else {
            size_t n = nlines - seq_pos < SEQ_CHUNK ? nlines - seq_pos : SEQ_CHUNK;
            writes += thresh ? seq_rw(seq_pos, n, thresh, &rng, tag) : seq_ro(seq_pos, n);
            ops += n;
            seq_pos = (seq_pos + n) % nlines;
        }
        t = now();
    } while (t - t0 < secs);

    sink += chase_pos;
    double el = t - t0;
    printf("OK pattern=%s ops=%lu secs=%.4f ns_per_op=%.3f gbps=%.3f writes=%lu\n",
           pattern, (unsigned long)ops, el, el * 1e9 / ops,
           ops * (double)LINE_SIZE / el / 1e9, (unsigned long)writes);
}

static size_t parse_size(const char *s)
{
    char *end;
    double v = strtod(s, &end);
    switch (*end) {
    case 'k': case 'K': v *= 1UL << 10; break;
    case 'm': case 'M': v *= 1UL << 20; break;
    case 'g': case 'G': v *= 1UL << 30; break;
    }
    return (size_t)v;
}

static void usage(const char *argv0)
{
    fprintf(stderr,
            "usage: %s --size BYTES[K|M|G] [--seed N] [--id 0|1] [--shared-frac F] [--no-madvise]\n"
            "Reads commands on stdin; normally launched by run_bench.py.\n", argv0);
    exit(2);
}

int main(int argc, char **argv)
{
    static const struct option opts[] = {
        {"size", required_argument, 0, 's'},
        {"seed", required_argument, 0, 'r'},
        {"id", required_argument, 0, 'i'},
        {"shared-frac", required_argument, 0, 'f'},
        {"no-madvise", no_argument, 0, 'n'},
        {0, 0, 0, 0},
    };
    int c;
    while ((c = getopt_long(argc, argv, "", opts, NULL)) != -1) {
        switch (c) {
        case 's': size_bytes = parse_size(optarg); break;
        case 'r': seed = strtoull(optarg, NULL, 0); break;
        case 'i': id = atoi(optarg); break;
        case 'f': shared_frac = atof(optarg); break;
        case 'n': do_madvise = 0; break;
        default: usage(argv[0]);
        }
    }
    size_bytes -= size_bytes % PAGE_SIZE;
    if (size_bytes < PAGE_SIZE)
        usage(argv[0]);
    nlines = size_bytes / LINE_SIZE;
    npages = size_bytes / PAGE_SIZE;
    setvbuf(stdout, NULL, _IOLBF, 0);

    char line[256], pattern[16];
    double secs, wfrac;
    while (fgets(line, sizeof(line), stdin)) {
        if (!strncmp(line, "fill", 4))
            cmd_fill();
        else if (!strncmp(line, "stat", 4))
            cmd_stat();
        else if (sscanf(line, "run %15s %lf %lf", pattern, &secs, &wfrac) == 3)
            cmd_run(pattern, secs, wfrac);
        else if (!strncmp(line, "quit", 4))
            break;
        else
            reply_err("unknown command: %s", line);
        fflush(stdout);
    }
    return 0;
}
