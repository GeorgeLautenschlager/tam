.PHONY: update-theseus

# Bump the pinned theseus tag and let poetry resolve/lock it.
#   make update-theseus VERSION=0.8.0
update-theseus:
	@test -n "$(VERSION)" || { echo "Usage: make update-theseus VERSION=X.Y.Z"; exit 1; }
	@echo "$(VERSION)" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$$' || { echo "VERSION must be X.Y.Z (got '$(VERSION)')"; exit 1; }
	@git ls-remote --exit-code --tags git@github.com:GeorgeLautenschlager/theseus.git "refs/tags/v$(VERSION)" >/dev/null || { echo "Tag v$(VERSION) not found on theseus remote."; exit 1; }
	env -u VIRTUAL_ENV poetry add "theseus @ git+ssh://git@github.com/GeorgeLautenschlager/theseus.git@v$(VERSION)"
	@echo "Pinned theseus to v$(VERSION). Restart the TAM_V3 tmux session to pick it up."
