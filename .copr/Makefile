# Invoked by COPR's "Build from SCM" source method: COPR checks out the
# triggering commit, cds into the repo root, and runs
# `make -f .copr/Makefile srpm outdir=<resultdir>`. The resulting .src.rpm
# must be left in $(outdir).
#
# To wire this up in COPR, create chroots for (at least):
#   fedora-43-x86_64, fedora-44-x86_64, fedora-45-x86_64,
#   epel-9-x86_64 (RHEL 9 / CentOS Stream 9),
#   epel-10-x86_64 (RHEL 10 / CentOS Stream 10)
# and set the source type to "SCM", Spec File = must-gather-sos.spec,
# Source directory = . (repo root).

name     := must-gather-sos
spec     := $(name).spec
version  := $(shell awk -F'[ \t]+' '/^Version:/ {print $$2; exit}' $(spec))
tarball  := $(name)-$(version).tar.gz
rpmbuild_topdir := $(CURDIR)/.rpmbuild

.PHONY: srpm

srpm: outdir ?= $(CURDIR)
srpm:
	rm -rf .rpmbuild
	mkdir -p .rpmbuild/SOURCES .rpmbuild/SPECS
		tar -czf .rpmbuild/SOURCES/$(NAME)-$(VERSION).tar.gz \
		--transform 's/^\./$(NAME)-$(VERSION)/' \
		--exclude='./.git' \
		--exclude='./.rpmbuild' \
	cp $(spec) .rpmbuild/SPECS/$(SPEC_BASENAME)
	rpmbuild -bs \
		--define "_topdir $(shell pwd)/.rpmbuild" \
		--define "_srcrpmdir $(outdir)" \
		.rpmbuild/SPECS/$(SPEC_BASENAME)
