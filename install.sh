#!/bin/bash
# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

# --- Configuration ---
INSTALL_DIR="$(pwd)/locai-link"
REPO_URL="https://github.com/locai-co-uk/locai-link.git"
BRANCH="main"

# --- Colors ---
GREEN='\033[0;32m'
BLUE='\033[0;34m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${BLUE}=== LocAI Edge Agent Installer ===${NC}"

# 1. Parse Arguments
DEVICE_NAME=""
USERNAME=""
REG_KEY=""
DEVICE_TYPE="edge_device"
API_URL=""

while [[ "$#" -gt 0 ]]; do
    case $1 in
        --device-name) DEVICE_NAME="$2"; shift ;;
        --username) USERNAME="$2"; shift ;;
        --registration-key) REG_KEY="$2"; shift ;;
        --device-type) DEVICE_TYPE="$2"; shift ;;
        --api-url) API_URL="$2"; shift ;;
        --branch) BRANCH="$2"; shift ;; # Added Branch Override
        *) echo "Unknown parameter passed: $1"; exit 1 ;;
    esac
    shift
done

if [[ -z "$DEVICE_NAME" || -z "$USERNAME" || -z "$REG_KEY" ]]; then
    echo -e "${RED}Error: Missing required arguments.${NC}"
    exit 1
fi

# 2. Check Prerequisites (Git & Python/uv)
echo -e "${BLUE}Checking system prerequisites...${NC}"

# CHECK: Ensure Git is installed
if ! command -v git &> /dev/null; then
    echo -e "${RED}Error: git is not installed. Please install git first.${NC}"
    echo "  Ubuntu/Debian: sudo apt-get install git"
    echo "  CentOS/RHEL:   sudo yum install git"
    echo "  macOS:         brew install git"
    exit 1
fi

# CHECK: Ensure uv is installed
if ! command -v uv &> /dev/null; then
    echo "uv not found. Installing..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    
    # Source the env to make uv available in this session
    if [ -f "$HOME/.local/bin/env" ]; then
        . "$HOME/.local/bin/env"
    elif [ -f "$HOME/.cargo/env" ]; then
        . "$HOME/.cargo/env"
    else
        export PATH="$HOME/.local/bin:$PATH"
    fi
else
    echo "✔ uv is already installed."
fi

# 3. Clone/Update Repository (Shallow)
if [ -d "$INSTALL_DIR" ]; then
    echo "Directory $INSTALL_DIR exists. Pulling latest changes..."
    cd "$INSTALL_DIR"
    git pull origin "$BRANCH"
else
    echo "Cloning repository to $INSTALL_DIR..."
    # CHANGE: Added --depth 1 for shallow clone
    git clone --depth 1 -b "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
    cd "$INSTALL_DIR"
fi

# 4. Run Setup via uv
echo -e "${BLUE}Initializing Environment...${NC}"
uv run manager.py setup

# 5. Register Device
echo -e "${BLUE}Registering device...${NC}"
CMD_ARGS=("register" "--device-name" "$DEVICE_NAME" "--username" "$USERNAME" "--registration-key" "$REG_KEY" "--device-type" "$DEVICE_TYPE")

if [[ -n "$API_URL" ]]; then
    CMD_ARGS+=("--api-url" "$API_URL")
fi

uv run manager.py "${CMD_ARGS[@]}"

if [ $? -eq 0 ]; then
    echo -e "${GREEN}✔ Device Registered Successfully!${NC}"
    echo -e "${BLUE}Starting Agent...${NC}"
    uv run manager.py run
else
    echo -e "${RED}❌ Registration failed.${NC}"
    exit 1
fi