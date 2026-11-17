#!/bin/sh
set -eu
exec waitress-serve --host=0.0.0.0 --port="${PORT:-10000}" app:app
