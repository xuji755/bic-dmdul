#!/bin/sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
VERSION=$(sed -n 's/^version = "\(.*\)"/\1/p' "$ROOT_DIR/pyproject.toml" | head -1)
if [ -z "$VERSION" ]; then
  echo "cannot read project version from pyproject.toml" >&2
  exit 1
fi

PRODUCT="bic-dmdul"
BUILD_DIR="$ROOT_DIR/tmp/release-build"
PACKAGE_DIR="$BUILD_DIR/$PRODUCT-$VERSION"
OUTPUT_DIR="$ROOT_DIR/releases"
OUTPUT="$OUTPUT_DIR/$PRODUCT-$VERSION.tar.gz"

rm -rf "$BUILD_DIR"
mkdir -p "$PACKAGE_DIR/bin" "$OUTPUT_DIR"

cp "$ROOT_DIR/pyproject.toml" "$PACKAGE_DIR/"
cp "$ROOT_DIR/README.md" "$PACKAGE_DIR/"
cp "$ROOT_DIR/LICENSE" "$PACKAGE_DIR/"
cp "$ROOT_DIR/NOTICE.md" "$PACKAGE_DIR/"
cp -R "$ROOT_DIR/src" "$PACKAGE_DIR/"
cp -R "$ROOT_DIR/docs" "$PACKAGE_DIR/"
cp -R "$ROOT_DIR/fixtures" "$PACKAGE_DIR/"
find "$PACKAGE_DIR" -type d -name __pycache__ -prune -exec rm -rf {} +
find "$PACKAGE_DIR" -type f -name '*.py[co]' -delete

cat > "$PACKAGE_DIR/VERSION" <<EOF
$PRODUCT $VERSION
EOF

cat > "$PACKAGE_DIR/bin/bic-dmdul" <<'EOF'
#!/bin/sh
BASE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PYTHONPATH="$BASE_DIR/src${PYTHONPATH:+:$PYTHONPATH}" exec python3 -m dmdul.cli "$@"
EOF
chmod 755 "$PACKAGE_DIR/bin/bic-dmdul"
ln -s bic-dmdul "$PACKAGE_DIR/bin/dmdul"

cat > "$PACKAGE_DIR/README_RELEASE_CN.md" <<EOF
# $PRODUCT $VERSION 发布包

运行方式：

\`\`\`sh
tar -xzf $PRODUCT-$VERSION.tar.gz
cd $PRODUCT-$VERSION
./bin/bic-dmdul --help
\`\`\`

本发布包包含源码、文档、命令行入口、LICENSE 和 NOTICE。
EOF

(cd "$BUILD_DIR" && tar -czf "$OUTPUT" "$PRODUCT-$VERSION")
sha256sum "$OUTPUT" > "$OUTPUT.sha256"

echo "$OUTPUT"
echo "$OUTPUT.sha256"
