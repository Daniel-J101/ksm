CC     ?= gcc
CFLAGS ?= -O3 -march=native -std=gnu11 -Wall -Wextra

ksm_bench: ksm_bench.c
	$(CC) $(CFLAGS) -o $@ $<

clean:
	rm -f ksm_bench

.PHONY: clean
