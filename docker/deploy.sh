#!/usr/bin/env bash
# 在本机构建并发布镜像；只在推送成功后更新 Compose 版本。
set -euo pipefail
DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$DEPLOY_DIR/.." && pwd)"
ENV_FILE="$DEPLOY_DIR/.env"
# 镜像仓库等部署环境信息只来自环境变量或 docker/.env（不进仓库，模板见 .env.example）。
env_value() {
    if [[ -f "$ENV_FILE" ]]; then sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1; fi
}
IMAGE="${IMAGE:-$(env_value IMAGE)}"
[[ -n "$IMAGE" ]] || { echo '需要 IMAGE：在环境变量或 docker/.env 中设置镜像仓库，见 docker/.env.example' >&2; exit 2; }
VERSION="${VERSION:-$(TZ=Asia/Shanghai date '+%Y.%m.%d.%H%M%S').$(git -C "$REPO_DIR" rev-parse --short HEAD).${RANDOM}}"
PLATFORM="${PLATFORM:-linux/amd64}"
OUTPUT_MODE="${OUTPUT_MODE:-push}"
[[ "$VERSION" =~ ^[a-zA-Z0-9_][a-zA-Z0-9_.-]*$ ]] || { echo '镜像版本不合法' >&2; exit 2; }
[[ "$IMAGE" =~ ^[a-zA-Z0-9./_-]+$ ]] || { echo '镜像仓库不合法' >&2; exit 2; }
[[ "$OUTPUT_MODE" == push || "$OUTPUT_MODE" == load ]] || { echo 'OUTPUT_MODE 必须是 push 或 load' >&2; exit 2; }
BUILD_CONTEXT="$(mktemp -d "${TMPDIR:-/tmp}/xuanpu-image.XXXXXX")"
trap 'rm -rf "$BUILD_CONTEXT"' EXIT
mkdir -p "$BUILD_CONTEXT/wheels"
cd "$REPO_DIR"
for package in xuanpu-spec xuanpu-server xuanpu-sdk-py xuanpu-cli xuanpu-console; do
    uv build --wheel "packages/$package" --out-dir "$BUILD_CONTEXT/wheels"
done
for package in taifeng-sandbox taifeng-stores; do
    uv build --wheel "$REPO_DIR/../$package" --out-dir "$BUILD_CONTEXT/wheels"
done
uv export --frozen --no-dev --all-packages --all-extras --no-emit-workspace \
    --no-emit-package taifeng-sandbox --no-emit-package taifeng-stores --no-editable \
    --output-file "$BUILD_CONTEXT/requirements.txt"
cp "$DEPLOY_DIR/Dockerfile" "$DEPLOY_DIR/trial_tickets.py" "$BUILD_CONTEXT/"
cp "$REPO_DIR/examples/ticket-api/server.py" "$BUILD_CONTEXT/ticket-api.py"
docker buildx build --platform "$PLATFORM" --file "$BUILD_CONTEXT/Dockerfile" \
    --tag "$IMAGE:$VERSION" --output type=docker "$BUILD_CONTEXT"
if [[ "$OUTPUT_MODE" == push ]]; then
    docker push "$IMAGE:$VERSION"
    # .env 只放镜像定位与部署环境名称，不放凭据；只替换 IMAGE、VERSION，其余项原样保留。
    {
        if [[ -f "$ENV_FILE" ]]; then grep -vE '^(IMAGE|VERSION)=' "$ENV_FILE" || true; fi
        printf 'IMAGE=%s\nVERSION=%s\n' "$IMAGE" "$VERSION"
    } > "$ENV_FILE.next"
    mv "$ENV_FILE.next" "$ENV_FILE"
    echo "镜像已发布：$IMAGE:$VERSION"
else
    echo "镜像已载入本地：${IMAGE}:${VERSION}；未更新服务器发布版本"
fi
