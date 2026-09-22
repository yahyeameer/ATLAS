#!/usr/bin/env bash
# Check the sandbox network: direct egress must fail, the proxy must allow
# allowlisted hosts and refuse everything else. Exit 0 only if all three hold.
set -uo pipefail

IMAGE="${CHECK_IMAGE:-curlimages/curl:latest}"
run() { docker run --rm --network atlas-agents "$IMAGE" "$@" >/dev/null 2>&1; }
fail=0

if run -sS --max-time 8 https://example.com; then
  echo "FAIL  direct egress reached example.com"; fail=1
else
  echo "PASS  direct egress is blocked"
fi
if run -sS --max-time 15 --proxy http://atlas-egress:3128 https://pypi.org/simple/; then
  echo "PASS  proxy allows pypi.org"
else
  echo "FAIL  proxy refused pypi.org"; fail=1
fi
if run -sS --max-time 8 --proxy http://atlas-egress:3128 https://example.com; then
  echo "FAIL  proxy allowed example.com"; fail=1
else
  echo "PASS  proxy refuses hosts off the allowlist"
fi
exit $fail
