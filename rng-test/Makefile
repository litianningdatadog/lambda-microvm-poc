# deploy-microvm.sh zips the app dir itself, so there is no artifact to build
# here. `build` is a fast pre-deploy gate: fail on broken JS before the slow,
# costly image build rather than after.
.PHONY: build clean

build:
	node --check server.js
	@echo "server.js: OK"

clean:
	rm -f code-artifact.zip
