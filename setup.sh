#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
VENV_PATH="${SCRIPT_DIR}/venv"
ROS_DISTRO="${ROS_DISTRO:-humble}"
ROS_SETUP="/opt/ros/${ROS_DISTRO}/setup.bash"
TAM_COMMON_COMMIT="4b19b608bdd8b6d6c60747e3fcfa1288399c3186"
TAM_COMMON_PATH="${TAM_COMMON_PATH:-${SCRIPT_DIR}/../TAM__common}"
ROS_PREFIX="$(dirname -- "${ROS_SETUP}")"

if [ -n "${COLCON_PREFIX_PATH:-}" ]; then
  IFS=':' read -r -a sourced_prefixes <<< "${COLCON_PREFIX_PATH}"
  for prefix in "${sourced_prefixes[@]}"; do
    if [ -n "${prefix}" ] && [ "${prefix}" != "${ROS_PREFIX}" ]; then
      echo "An external colcon workspace is already sourced: ${prefix}" >&2
      echo "Run setup.sh from a clean shell so the pinned build is isolated." >&2
      exit 1
    fi
  done
  unset sourced_prefixes prefix
fi

if [ ! -d "${TAM_COMMON_PATH}" ]; then
  echo "TAM__common not found at ${TAM_COMMON_PATH}." >&2
  echo "Import the pinned dependency with:" >&2
  echo "  vcs import \"$(dirname -- "${SCRIPT_DIR}")\" < \"${SCRIPT_DIR}/dependencies.repos\"" >&2
  echo "  git -C \"${TAM_COMMON_PATH}\" submodule update --init --recursive" >&2
  echo "Or set TAM_COMMON_PATH to an existing checkout." >&2
  exit 1
fi
TAM_COMMON_PATH="$(cd -- "${TAM_COMMON_PATH}" && pwd -P)"

if ! git -C "${TAM_COMMON_PATH}" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "TAM_COMMON_PATH is not a Git checkout: ${TAM_COMMON_PATH}" >&2
  exit 1
fi

actual_commit="$(git -C "${TAM_COMMON_PATH}" rev-parse HEAD)"
if [ "${actual_commit}" != "${TAM_COMMON_COMMIT}" ]; then
  echo "TAM__common must be at ${TAM_COMMON_COMMIT}; found ${actual_commit}." >&2
  exit 1
fi

if ! submodule_status="$(git -C "${TAM_COMMON_PATH}" submodule status --recursive)"; then
  echo "Could not inspect TAM__common submodules at ${TAM_COMMON_PATH}." >&2
  exit 1
fi
while IFS= read -r status; do
  case "${status}" in
    -*|+*|U*)
      echo "TAM__common submodules are not recursively initialized at their pinned revisions." >&2
      echo "Run: git -C \"${TAM_COMMON_PATH}\" submodule update --init --recursive" >&2
      exit 1
      ;;
  esac
done <<< "${submodule_status}"

if [ ! -f "${ROS_SETUP}" ]; then
  echo "ROS 2 setup file not found at ${ROS_SETUP}." >&2
  exit 1
fi

if [ -e "${VENV_PATH}" ] && [ ! -f "${VENV_PATH}/bin/activate" ]; then
  echo "Existing path is not a usable virtual environment: ${VENV_PATH}" >&2
  exit 1
fi
if [ ! -d "${VENV_PATH}" ]; then
  echo "Creating virtual environment at ${VENV_PATH}..."
  python3 -m venv "${VENV_PATH}"
else
  echo "Reusing virtual environment at ${VENV_PATH}."
fi

set +u
. "${ROS_SETUP}"
. "${VENV_PATH}/bin/activate"
set -u

python -m pip install -r "${SCRIPT_DIR}/requirements.txt"
touch "${VENV_PATH}/COLCON_IGNORE"

COLCON_ROOT="${VENV_PATH}/colcon"
echo "Building tum_types_py and vehicle_handler_py from ${TAM_COMMON_PATH}..."
colcon --log-base "${COLCON_ROOT}/log" build \
  --build-base "${COLCON_ROOT}/build" \
  --install-base "${COLCON_ROOT}/install" \
  --base-paths "${TAM_COMMON_PATH}" \
  --cmake-clean-cache \
  --packages-up-to tum_types_py vehicle_handler_py

echo "Setup completed successfully."
echo "For each new shell, run:"
echo "  source \"${VENV_PATH}/bin/activate\""
echo "  source \"${COLCON_ROOT}/install/setup.bash\""
