.PHONY: check checker-test functional-test roles-test image-check official-check role-images role-images-full

OFFICIAL_IMAGE ?= quay.io/ceph/ceph:v20.2.4@sha256:6bb1c8a42fbc0bf87938946990b65174466997bc11c31eb5a323225a779fd8f9
CEPH_SOURCE_IMAGE ?= $(OFFICIAL_IMAGE)
ROLE_REPOSITORY ?= ceph-testcontainers

# Host unit tests only; no Docker.
check: checker-test functional-test roles-test

checker-test:
	python3 -m unittest discover -s image/tests -v

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
