.PHONY: check checker-test functional-test roles-test image-check official-check role-images role-images-full debian-images ubuntu-images

# Release inputs live in image/releases.py; CEPH_RELEASE selects one of them.
CEPH_RELEASE ?= 20.2.4
OFFICIAL_IMAGE ?= $(shell python3 image/releases.py official $(CEPH_RELEASE))
CEPH_SOURCE_IMAGE ?= $(OFFICIAL_IMAGE)
ROLE_REPOSITORY ?= ceph-testcontainers

# Host unit tests only; no Docker.
check: checker-test functional-test roles-test

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

debian-images ubuntu-images: %-images:
	build_args="$$(python3 image/releases.py build-args $(CEPH_RELEASE) $*)" || exit 1; \
	for role in control osd rgw mds all; do \
		docker buildx build --load --platform "$(PLATFORM)" --provenance=false --target $$role $$build_args \
			-t "$(IMAGE_REPOSITORY):$*-$(CEPH_RELEASE)-$$role" image/$* || exit 1; \
	done
