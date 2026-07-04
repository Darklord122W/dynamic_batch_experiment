#!/usr/bin/env bash
# =============================================================================
# download_yolo11n.sh
#
# Purpose:
#   Acquire the PGIE detector for the multicam_perception pipeline.
#   Steps:
#     1. Fetch the DeepStream-Yolo helper repo (ONNX exporter + custom bbox parser).
#     2. In a throwaway Python venv, install Ultralytics + ONNX tooling.
#     3. Download the pretrained YOLO11n weights and export them to ONNX in the
#        exact layout the DeepStream custom parser expects (boxes + score + label).
#     4. Compile the custom bbox-parser plugin (libnvdsinfer_custom_impl_Yolo.so)
#        against the installed DeepStream SDK.
#     5. Drop the resulting model.onnx, labels.txt and the parser .so into
#        multicam_perception/models/ where config/pgie_config.txt points.
#
#   The TensorRT *.engine is NOT built here on purpose: nvinfer builds it on the
#   first pipeline launch (or run scripts/build_engine.sh), so the engine matches
#   the exact TensorRT/GPU on this device.
#
#   Export runs on CPU only, so the venv never touches the system Python or the
#   DeepStream runtime — the sole artifacts consumed at runtime are the ONNX,
#   labels.txt and the compiled .so.
#
# Tested on: Jetson AGX Orin, JetPack 6.2.x (L4T r36.5), DeepStream 7.1,
#            CUDA 12.6, TensorRT 10.3, Python 3.10.
#
# Usage:
#   ./scripts/download_yolo11n.sh            # defaults: yolo11n, 640x640
#   MODEL=yolo11s IMG_SIZE=640 ./scripts/download_yolo11n.sh
# =============================================================================
set -euo pipefail

# ---- Tunables (override via environment) ------------------------------------
MODEL="${MODEL:-yolo11n}"                 # ultralytics model stem
IMG_SIZE="${IMG_SIZE:-640}"               # square inference size (YOLO11 default 640)
CUDA_VER="${CUDA_VER:-12.6}"              # must match the DeepStream build (DS7.1 Jetson = 12.6)
DS_YOLO_REF="${DS_YOLO_REF:-master}"      # DeepStream-Yolo git ref

# ---- Resolve paths ----------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
MODELS_DIR="${PROJECT_DIR}/models"
WORK_DIR="${MODELS_DIR}/_build"           # scratch clone + venv live here
DS_YOLO_DIR="${WORK_DIR}/DeepStream-Yolo"
VENV_DIR="${WORK_DIR}/export-venv"

mkdir -p "${MODELS_DIR}" "${WORK_DIR}"

echo "==> Project:  ${PROJECT_DIR}"
echo "==> Model:    ${MODEL}  (input ${IMG_SIZE}x${IMG_SIZE})"
echo "==> CUDA_VER: ${CUDA_VER}"

# ---- 1. DeepStream-Yolo repo (exporter + parser source) ---------------------
if [ ! -d "${DS_YOLO_DIR}/.git" ]; then
  echo "==> Cloning DeepStream-Yolo ..."
  git clone --depth 1 --branch "${DS_YOLO_REF}" \
    https://github.com/marcoslucianops/DeepStream-Yolo.git "${DS_YOLO_DIR}"
else
  echo "==> DeepStream-Yolo already present, reusing."
fi

# ---- 2. Export venv (CPU-only; isolated from system + DeepStream python) -----
# Prefer stdlib venv; fall back to the `virtualenv` tool on stripped-down JetPack
# images where python3-venv / ensurepip is not installed (avoids needing sudo).
if [ ! -d "${VENV_DIR}" ]; then
  echo "==> Creating export venv ..."
  if python3 -m venv "${VENV_DIR}" 2>/dev/null; then
    :
  else
    echo "    stdlib venv unavailable (no python3-venv); using virtualenv ..."
    python3 -m virtualenv --version >/dev/null 2>&1 || pip3 install --user virtualenv
    python3 -m virtualenv "${VENV_DIR}"
  fi
fi
# shellcheck disable=SC1091
source "${VENV_DIR}/bin/activate"
python -m pip install --upgrade pip wheel >/dev/null
# CPU torch is all the exporter needs; ultralytics pulls a matching torch wheel.
echo "==> Installing ultralytics + onnx tooling (this can take a few minutes) ..."
# onnxscript is imported unconditionally by torch>=2.9's onnx export machinery,
# even for the legacy exporter, so it must be present.
# NOTE: onnxslim/onnxruntime are intentionally omitted — onnxruntime aborts on the
# Tegra CPU (cpuinfo can't identify the vendor), and simplification is unnecessary
# because TensorRT re-optimizes the graph when it builds the engine.
pip install "ultralytics>=8.3.0" onnx onnxscript

# ---- 3. Download weights + export to DeepStream ONNX -------------------------
cd "${WORK_DIR}"
cp "${DS_YOLO_DIR}/utils/export_yolo11.py" ./export_yolo11.py
# Force torch's LEGACY (TorchScript) ONNX exporter. torch>=2.9 defaults to the new
# dynamo/torch.export path, which does not reproduce the DeepStream-Yolo custom
# output graph (transpose + boxes/score/label) that libnvdsinfer_custom_impl_Yolo
# parses. dynamo=False guarantees the exact graph the parser expects.
if ! grep -q "dynamo=False" export_yolo11.py; then
  sed -i 's/^        verbose=False,/        verbose=False,\n        dynamo=False,/' export_yolo11.py
fi

if [ ! -f "${MODEL}.pt" ]; then
  echo "==> Downloading ${MODEL}.pt via Ultralytics (auto-resolves correct release tag) ..."
  python - "${MODEL}" <<'PY'
import sys
from ultralytics import YOLO
# Instantiating with a bare name triggers Ultralytics' own asset download,
# which is robust to release-tag changes (no hard-coded GitHub URL to rot).
YOLO(f"{sys.argv[1]}.pt")
PY
fi

echo "==> Exporting ${MODEL}.pt -> ONNX (dynamic batch) ..."
# --dynamic : one engine serves batch 1..N (N = camera count, set in pgie_config).
# opset 17 default is fine for TensorRT 10.3 / DeepStream 7.1.
# (No --simplify: see the onnxslim note above.)
python export_yolo11.py -w "${MODEL}.pt" -s "${IMG_SIZE}" --dynamic

# ---- 4. Compile the custom bbox parser against the installed DeepStream ------
echo "==> Building libnvdsinfer_custom_impl_Yolo.so (CUDA_VER=${CUDA_VER}) ..."
make -C "${DS_YOLO_DIR}/nvdsinfer_custom_impl_Yolo" clean || true
CUDA_VER="${CUDA_VER}" make -C "${DS_YOLO_DIR}/nvdsinfer_custom_impl_Yolo"

# ---- 5. Publish artifacts into models/ --------------------------------------
echo "==> Publishing artifacts to ${MODELS_DIR} ..."
cp -f "${WORK_DIR}/${MODEL}.onnx" "${MODELS_DIR}/${MODEL}.onnx"
cp -f "${WORK_DIR}/labels.txt"    "${MODELS_DIR}/labels.txt"
mkdir -p "${MODELS_DIR}/nvdsinfer_custom_impl_Yolo"
cp -f "${DS_YOLO_DIR}/nvdsinfer_custom_impl_Yolo/libnvdsinfer_custom_impl_Yolo.so" \
      "${MODELS_DIR}/nvdsinfer_custom_impl_Yolo/libnvdsinfer_custom_impl_Yolo.so"

deactivate || true

cat <<EOF

==> DONE.
    Artifacts in ${MODELS_DIR}:
      - ${MODEL}.onnx
      - labels.txt                       ($(wc -l < "${MODELS_DIR}/labels.txt") classes)
      - nvdsinfer_custom_impl_Yolo/libnvdsinfer_custom_impl_Yolo.so

    config/pgie_config.txt already points at these paths.
    The TensorRT engine builds automatically on first pipeline launch
    (can take several minutes the first time).
EOF
