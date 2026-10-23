.PHONY: check base-check checker-test functional-test roles-test extension-test extension-images extension-selectors extension-check capture-image capture-check capture-functional-check image-check official-check role-images role-images-full debian-images ubuntu-images alpine-images
CEPH_RELEASE ?= 20.2.4
OFFICIAL_IMAGE ?= $(shell python3 image/releases.py official $(CEPH_RELEASE))
IMAGE_REPOSITORY ?= ceph-testcontainers
PLATFORM ?= linux/amd64
VARIANT ?= official
ROLE_TAG = $(VARIANT)-$(if $(filter alpine,$(VARIANT)),19.2.4,$(CEPH_RELEASE))
ROLE_SOURCE = $(IMAGE_REPOSITORY):$(ROLE_TAG)-source
ROLE_CHECK ?= quick
ROLE_OUTPUT ?= artifacts/$(ROLE_TAG)-$(subst /,-,$(PLATFORM))

check: base-check extension-test
base-check: checker-test functional-test roles-test
checker-test:
	python3 -m unittest discover -s image/tests -v
	python3 image/releases.py official $(CEPH_RELEASE) >/dev/null
functional-test:
	python3 -m unittest discover -s image/functional/tests -v
roles-test:
	python3 -m unittest discover -s image/roles/tests -v
extension-test:
	python3 -m unittest discover -s image/extensions/tests -v
image-check:
	python3 image/check.py $(IMAGE_CHECK_ARGS)
official-check:
	python3 image/check.py --image "all=$(OFFICIAL_IMAGE)" --full

# Distribution Dockerfiles provide an installed source runtime. Only the
# dependency assembler below builds the published role images.
role-images:
	@case "$(VARIANT)" in official|debian|ubuntu|alpine) ;; *) exit 2 ;; esac
	@if [ "$(VARIANT)" = official ]; then \
		source="$(OFFICIAL_IMAGE)"; backend=rpm; docker pull --platform "$(PLATFORM)" "$$source" || exit 1; \
	else \
		build_args=""; backend=deb; \
		if [ "$(VARIANT)" = alpine ]; then backend=apk; else \
			build_args="$$(python3 image/releases.py build-args $(CEPH_RELEASE) $(VARIANT))" || exit 1; fi; \
		$(call registry_cache,-source) \
		docker buildx build --load --platform "$(PLATFORM)" --provenance=false --target all $$build_args $$cache_args \
			-t "$(ROLE_SOURCE)" image/$(VARIANT) || exit 1; source="$(ROLE_SOURCE)"; \
	fi; \
	python3 image/roles/build.py --package-backend $$backend --source-runtime-env \
		--source-image "$$source" --skip-pull --platform "$(PLATFORM)" --repository "$(IMAGE_REPOSITORY)" \
		--tag "$(ROLE_TAG)" --check $(ROLE_CHECK) --output-dir "$(ROLE_OUTPUT)"

role-images-full:
	$(MAKE) role-images ROLE_CHECK=full

debian-images ubuntu-images alpine-images: %-images:
	$(MAKE) role-images VARIANT=$*

# The optional extension uses a separate build cache and publication contract.
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

# Opt-in extension helpers, built and checked apart from the role images
# (docs/EXTENSIONS.md). The check needs NET_ADMIN for its own helper containers.
EXTENSION_IMAGE ?= $(IMAGE_REPOSITORY):extension-netem

extension-images:
	$(call registry_cache) \
	docker buildx build --load --platform "$(PLATFORM)" --provenance=false $$cache_args \
		-t "$(IMAGE_REPOSITORY):extension-netem" image/extensions/netem

# The packet-capture helper has its own image, a quick and a functional check.
CAPTURE_IMAGE ?= $(IMAGE_REPOSITORY):extension-capture
CAPTURE_TARGET ?= $(OFFICIAL_IMAGE)

capture-image:
	$(call registry_cache) \
	docker buildx build --load --platform "$(PLATFORM)" --provenance=false $$cache_args \
		-t "$(IMAGE_REPOSITORY):extension-capture" image/extensions/capture

# Example: make capture-check CAPTURE_IMAGE=my-registry/capture-helper:dev
capture-check:
	python3 image/extension_check.py --extension capture --image "$(CAPTURE_IMAGE)"

# Captures in the namespace of a container of CAPTURE_TARGET; needs NET_RAW for
# its own helper containers only.
capture-functional-check:
	python3 image/extension_check.py --extension capture --image "$(CAPTURE_IMAGE)" \
		--functional --target "$(CAPTURE_TARGET)"

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
