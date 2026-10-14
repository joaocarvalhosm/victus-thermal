# SPDX-License-Identifier: GPL-2.0-or-later
#
# Out-of-tree build of hp-wmi with HP Victus 15-fb2xxx (board 8C2F) support.
# DKMS calls the "modules" target with KERNELRELEASE set; for a manual build
# against the running kernel just run `make`.

obj-m := hp-wmi.o

KVER ?= $(shell uname -r)
KDIR ?= /lib/modules/$(KVER)/build

all: modules

modules:
	$(MAKE) -C $(KDIR) M=$(CURDIR) modules

clean:
	$(MAKE) -C $(KDIR) M=$(CURDIR) clean

.PHONY: all modules clean
