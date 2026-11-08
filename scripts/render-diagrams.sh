#!/usr/bin/env bash
# Renders docs/c4/*.puml to docs/diagrams/*.svg and *.png.
# Uses $PLANTUML_JAR (java -jar) when set, otherwise the plantuml command.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p docs/diagrams
if [ -n "${PLANTUML_JAR:-}" ]; then PUML=(java -jar "$PLANTUML_JAR"); else PUML=(plantuml); fi
"${PUML[@]}" -tsvg -o "$PWD/docs/diagrams" docs/c4/*.puml
"${PUML[@]}" -tpng -o "$PWD/docs/diagrams" docs/c4/*.puml
ls -1 docs/diagrams
