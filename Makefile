.PHONY: check base-check extension-test checker-test functional-test roles-test image-check official-check role-images role-images-full debian-images ubuntu-images extension-images extension-selectors extension-check

# Release inputs live in image/releases.py; CEPH_RELEASE selects one of them.
CEPH_RELEASE ?= 20.2.4
OFFICIAL_IMAGE ?= $(shell python3 image/releases.py official $(CEPH_RELEASE))
CEPH_SOURCE_IMAGE ?= $(OFFICIAL_IMAGE)
ROLE_REPOSITORY ?= ceph-testcontainers

# Host unit tests only; no Docker. Release images depend only on base-check,
# so that an extension defect never blocks them.
check: base-check extension-test

base-check: checker-test functional-test roles-test

extension-test:
	python3 -m unittest discover -s image/extensions/tests -v

checker-test:
	python3 -m unittest discover -s image/tests -v
	python3 image/releases.py official $(CEPH_RELEASE) >/dev/null

functional-test:
	python3 -m unittest discover -s image/functional/tests -v

roles-test:
	python3 -m unittest discover -s image/roles/tests -v

# Example: make image-check IMAGE_CHECK_ARGS='--image all=my-ceph:dev --full'
image-check:
	python3 image/check.py $(IMAGE_CHECK_ARGS)

# Quick and full check of the official image; it must always pass.
official-check:
	python3 image/check.py --image "all=$(OFFICIAL_IMAGE)" --full

# Role images extracted from the official image.
role-images:
	python3 image/roles/build.py --source-image "$(CEPH_SOURCE_IMAGE)" --repository "$(ROLE_REPOSITORY)" --check quick

role-images-full:
	python3 image/roles/build.py --source-image "$(CEPH_SOURCE_IMAGE)" --repository "$(ROLE_REPOSITORY)" --check full

# Alternative role images built from distribution packages.
IMAGE_REPOSITORY ?= ceph-testcontainers
PLATFORM ?= linux/arm64

# Optional registry cache, separate from the tested/published image tags.
# Use a docker-container Buildx builder for registry cache export. Each role
# gets its own stable tag so the five exports cannot overwrite one another.
BUILD_CACHE_REF ?=
BUILD_CACHE_WRITE ?= false

define registry_cache
cache_args=""; \
if [ -n "$(BUILD_CACHE_REF)" ]; then \
	cache_ref="$(BUILD_CACHE_REF)$(1)"; \
	cache_args="--cache-from=type=registry,ref=$$cache_ref"; \
	if [ "$(BUILD_CACHE_WRITE)" = true ]; then \
		cache_args="$$cache_args --cache-to=type=registry,ref=$$cache_ref,mode=max,ignore-error=true"; \
	fi; \
fi;
endef

debian-images ubuntu-images: %-images:
	build_args="$$(python3 image/releases.py build-args $(CEPH_RELEASE) $*)" || exit 1; \
	for role in control osd rgw mds all; do \
		$(call registry_cache,-$$role) \
		docker buildx build --load --platform "$(PLATFORM)" --provenance=false --target $$role $$build_args \
			$$cache_args -t "$(IMAGE_REPOSITORY):$*-$(CEPH_RELEASE)-$$role" image/$* || exit 1; \
	done

# Opt-in extension helpers, built and checked apart from the role images
# (docs/EXTENSIONS.md). The check needs NET_ADMIN for its own helper containers.
EXTENSION_IMAGE ?= $(IMAGE_REPOSITORY):extension-netem

extension-images:
	$(call registry_cache) \
	docker buildx build --load --platform "$(PLATFORM)" --provenance=false $$cache_args \
		-t "$(IMAGE_REPOSITORY):extension-netem" image/extensions/netem

# The selector profiles of the netem contract, one per line; CI checks them all.
EXTENSION_SELECTORS = python3 -c 'import sys; sys.path.insert(0, "image"); import extension_check; print("\n".join(extension_check.SELECTORS))'

extension-selectors:
	@$(EXTENSION_SELECTORS)

# Every selector profile and the optional bandwidth (--rate) and packet-fault
# (--packet-faults) profiles.
# Example: make extension-check EXTENSION_IMAGE=my-registry/netem-helper:dev
extension-check:
	python3 image/extension_check.py --extension netem --image "$(EXTENSION_IMAGE)" \
		--functional --target "$(OFFICIAL_IMAGE)" $(addprefix --selector ,$(shell $(EXTENSION_SELECTORS))) --rate --packet-faults
