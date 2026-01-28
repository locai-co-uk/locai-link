#!/bin/bash
# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

# --- Configuration ---
REPO_URL="https://github.com/locai-co-uk/locai-link.git"
BRANCH="main"
PYTHON_VERSION="3.11.8"

# --- Colors ---
GREEN='\033[0;32m'
BLUE='\033[0;34m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${BLUE}=== LocAI Edge Agent Installer ===${NC}"

# --- Check if already inside the repository ---
if [ -f "pyproject.toml" ]; then
    echo -e "${GREEN}Detected running inside repository.${NC}"
    echo "Skipping Git clone/pull. Using current directory."
    INSTALL_DIR="$(pwd)"
    SKIP_GIT=true
else
    INSTALL_DIR="$(pwd)/locai-link"
    SKIP_GIT=false
fi

# 1. Parse Arguments
DEVICE_NAME=""
USERNAME=""
REG_KEY=""
DEVICE_TYPE="edge_device"
API_URL=""
START_RUNNING=false

while [[ "$#" -gt 0 ]]; do
    case $1 in
        --device-name) DEVICE_NAME="$2"; shift ;;
        --username) USERNAME="$2"; shift ;;
        --registration-key) REG_KEY="$2"; shift ;;
        --device-type) DEVICE_TYPE="$2"; shift ;;
        --api-url) API_URL="$2"; shift ;;
        --branch) BRANCH="$2"; shift ;;
        --start-running) START_RUNNING=true ;;
        *) echo "Unknown parameter passed: $1"; exit 1 ;;
    esac
    shift
done

# --- Interactive Prompts ---
if [[ -z "$DEVICE_NAME" ]]; then read -p "Enter Device Name: " DEVICE_NAME; fi
if [[ -z "$USERNAME" ]]; then read -p "Enter Username: " USERNAME; fi
if [[ -z "$REG_KEY" ]]; then read -p "Enter Registration Key: " REG_KEY; fi

# 2. Check Prerequisites (Git & Python/uv)
echo -e "\n${BLUE}Checking system prerequisites...${NC}"

if [ "$SKIP_GIT" = false ]; then
    if ! command -v git &> /dev/null; then
        echo -e "${RED}Error: git is not installed.${NC}"
        exit 1
    fi
fi

if ! command -v uv &> /dev/null; then
    echo "uv not found. Installing..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    if [ -f "$HOME/.local/bin/env" ]; then . "$HOME/.local/bin/env"; 
    elif [ -f "$HOME/.cargo/env" ]; then . "$HOME/.cargo/env"; 
    else export PATH="$HOME/.local/bin:$PATH"; fi
else
    echo "✔ uv is already installed."
fi

# 3. Clone/Update Repository
if [ "$SKIP_GIT" = false ]; then
    if [ -d "$INSTALL_DIR" ]; then
        echo "Updating repository in $INSTALL_DIR..."
        cd "$INSTALL_DIR" || exit
        git pull origin "$BRANCH"
    else
        echo "Cloning repository to $INSTALL_DIR..."
        git clone --depth 1 -b "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
        cd "$INSTALL_DIR" || exit
    fi
else
    cd "$INSTALL_DIR" || exit
fi

# --- Load Defaults from Repo ---
if [ -f "defaults.env" ]; then
    set -a; source "defaults.env"; set +a
fi
DEFAULT_API_URL=${DEFAULT_API_URL:-"https://api.locai.co.uk/api/v1"}
DEV_API_URL=${DEV_API_URL:-"https://dev-api.locai.co.uk/api/v1"}
LOCAL_API_URL=${LOCAL_API_URL:-"http://localhost:8001/api/v1"}

# --- API URL Selection ---
if [[ -z "$API_URL" ]]; then
    echo -e "\n${BLUE}Select API Environment:${NC}"
    echo "1) Production ($DEFAULT_API_URL)"
    echo "2) Dev        ($DEV_API_URL)"
    echo "3) Localhost  ($LOCAL_API_URL)"
    echo "4) Custom URL"
    read -p "Choice [1]: " API_CHOICE
    case $API_CHOICE in
        2) API_URL="$DEV_API_URL" ;;
        3) API_URL="$LOCAL_API_URL" ;;
        4) read -p "Enter Custom API URL: " API_URL ;;
        *) API_URL="$DEFAULT_API_URL" ;;
    esac
fi

# 4. Environment Setup (Python 3.11.8)
echo -e "\n${BLUE}Initializing Environment (Python $PYTHON_VERSION)...${NC}"

# Ensure specific python version
uv python install "$PYTHON_VERSION"
# Create/Recreate Virtual Environment explicitly
rm -rf .venv
uv venv --python "$PYTHON_VERSION"

# 5. Run Manager Setup (Installs Inference Engine + Dependencies)
echo -e "\n${BLUE}Running internal setup...${NC}"
uv run manager.py setup

# 6. Register Device
echo -e "\n${BLUE}Registering device...${NC}"
CMD_ARGS=("register" "--device-name" "$DEVICE_NAME" "--username" "$USERNAME" "--registration-key" "$REG_KEY" "--device-type" "$DEVICE_TYPE" "--api-url" "$API_URL")
uv run manager.py "${CMD_ARGS[@]}"

if [ $? -eq 0 ]; then
    echo -e "${GREEN}✔ Device Registered Successfully!${NC}"
    
    SHOULD_START=$START_RUNNING
    if [ "$START_RUNNING" = false ]; then
        echo ""
        read -p "Do you want to start the agent now? [Y/n] " START_CONFIRM
        if [[ "$START_CONFIRM" =~ ^[Yy]$ || -z "$START_CONFIRM" ]]; then
            SHOULD_START=true
        fi
    fi

    if [ "$SHOULD_START" = true ]; then
        echo -e "${BLUE}Starting Agent...${NC}"
        uv run manager.py run
    else
        echo "Setup complete. To run the agent later, use:"
        echo "  cd $INSTALL_DIR && uv run manager.py run"
    fi
else
    echo -e "${RED}❌ Registration failed.${NC}"
    exit 1
fi