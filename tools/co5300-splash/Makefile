# SPDX-License-Identifier: MIT
CFLAGS ?= -Os

co5300-splash: co5300-splash.c
	$(CC) -std=c11 -D_POSIX_C_SOURCE=200809L -Wall -Wextra $(CFLAGS) $(LDFLAGS) -o $@ $<

.PHONY: clean
clean:
	rm -f co5300-splash
