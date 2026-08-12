#!/bin/bash

# PSA Backend Deployment Script
# Deploys gateway.py (portal, admin-dashboard and portal-dashboard FastAPI
# backends) via PM2

set -e

GREEN='\033[0;32m'
BLUE='\033[0;34m'
RED='\033[0;31m'
NC='\033[0m'

print_status()  { echo -e "${BLUE}[INFO]${NC} $1"; }
print_success() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }
print_error()   { echo -e "${RED}[ERROR]${NC} $1"; }

# ── Pre-flight checks ────────────────────────────────────────────────────────

if [ ! -f portal-frontend-code/.env ]; then
    print_error "portal-frontend-code/.env not found. Create one based on portal-frontend-code/.env.example"
    exit 1
fi

if [ ! -f admin-dashboard/.env ]; then
    print_error "admin-dashboard/.env not found. Create one based on admin-dashboard/.env.example"
    exit 1
fi

# Every backend reads DB_HOST/DB_USER out of the environment at import time, so a
# missing .env is not a lazy failure — gateway.py raises KeyError before the
# first request and PM2 restart-loops.
if [ ! -f portal-dashboard/.env ]; then
    print_error "portal-dashboard/.env not found. Create one based on portal-dashboard/.env.example"
    exit 1
fi

if ! command -v pm2 &> /dev/null; then
    print_error "PM2 is not installed. Run: npm install -g pm2"
    exit 1
fi

if ! command -v python3 &> /dev/null; then
    # Check if a previous source build already installed it
    if [ -x /usr/local/bin/python3.11 ]; then
        print_status "python3 not on PATH but /usr/local/bin/python3.11 found — symlinking..."
        sudo ln -sf /usr/local/bin/python3.11 /usr/bin/python3
        hash -r 2>/dev/null || true
    else
        print_error "python3 is not installed or not on PATH"
        exit 1
    fi
fi

# FastAPI 0.115 requires Python 3.8+
PYTHON_MINOR=$(python3 -c "import sys; print(sys.version_info.minor)")
PYTHON_MAJOR=$(python3 -c "import sys; print(sys.version_info.major)")
if [ "$PYTHON_MAJOR" -lt 3 ] || { [ "$PYTHON_MAJOR" -eq 3 ] && [ "$PYTHON_MINOR" -lt 8 ]; }; then
    print_status "Python 3.8+ required (found $(python3 --version)), installing Python 3.11..."

    if command -v apt-get &> /dev/null; then
        print_status "Using apt (Ubuntu/Debian)..."
        sudo apt-get install -y software-properties-common
        sudo add-apt-repository -y ppa:deadsnakes/ppa
        sudo apt-get update -y
        sudo apt-get install -y python3.11 python3.11-pip python3.11-venv
        sudo update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1

    elif command -v yum &> /dev/null; then
        print_status "Using yum — trying package install first..."
        sudo yum install -y python3.11 python3.11-pip > /dev/null 2>&1 || {
            PYTHON_BUILD_LOG="/tmp/python311-build.log"
            print_status "python3.11 not in yum repos, compiling from source (takes ~5 min)..."
            print_status "Build output → $PYTHON_BUILD_LOG"
            {
                sudo yum groupinstall -y "Development Tools"
                sudo yum install -y openssl-devel bzip2-devel libffi-devel xz-devel
                ORIG_DIR=$(pwd)
                cd /tmp
                curl -sO https://www.python.org/ftp/python/3.11.9/Python-3.11.9.tgz
                tar xzf Python-3.11.9.tgz
                cd Python-3.11.9
                ./configure --enable-optimizations --prefix=/usr/local
                make -j"$(nproc)" altinstall
                cd "$ORIG_DIR"
                sudo ln -sf /usr/local/bin/python3.11 /usr/bin/python3
            } > "$PYTHON_BUILD_LOG" 2>&1 || {
                print_error "Python 3.11 build failed. Check $PYTHON_BUILD_LOG for details."
                exit 1
            }
            # Re-create symlink outside the redirect block — alternatives may have clobbered it
            sudo ln -sf /usr/local/bin/python3.11 /usr/bin/python3
            print_success "Python 3.11 compiled successfully"
        }

    elif command -v dnf &> /dev/null; then
        print_status "Using dnf (Amazon Linux 2023 / Fedora)..."
        sudo dnf install -y python3.11 python3.11-pip
        sudo alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1

    else
        print_error "No supported package manager found (apt/yum/dnf). Install Python 3.11 manually and re-run."
        exit 1
    fi

    # hash -r forces the shell to re-resolve python3 from the new symlink
    hash -r 2>/dev/null || true

    PYTHON_BIN=$(command -v python3 || command -v python3.11 || echo "/usr/local/bin/python3.11")
    PYTHON_MINOR=$("$PYTHON_BIN" -c "import sys; print(sys.version_info.minor)")
    PYTHON_MAJOR=$("$PYTHON_BIN" -c "import sys; print(sys.version_info.major)")
    if [ "$PYTHON_MAJOR" -lt 3 ] || { [ "$PYTHON_MAJOR" -eq 3 ] && [ "$PYTHON_MINOR" -lt 8 ]; }; then
        print_error "Python upgrade failed (still on $("$PYTHON_BIN" --version)). Install manually and re-run."
        exit 1
    fi

    print_success "Python upgraded to $("$PYTHON_BIN" --version)"
fi

hash -r 2>/dev/null || true
print_status "Pre-flight checks passed ✓ ($(python3 --version))"

# ── Python dependencies ──────────────────────────────────────────────────────

# Ensure site-packages from the installed Python are on the module search path
PYTHON_LOCAL_LIB=$(python3 -c "import sys; print([p for p in sys.path if 'site-packages' in p][0])" 2>/dev/null || echo "")
if [ -n "$PYTHON_LOCAL_LIB" ]; then
    export PYTHONPATH="$PYTHON_LOCAL_LIB:${PYTHONPATH:-}"
    print_status "PYTHONPATH set to $PYTHON_LOCAL_LIB"
fi

if ! command -v pip3 &> /dev/null && ! command -v pip &> /dev/null; then
    print_status "pip not found, installing via get-pip.py..."
    curl -sS https://bootstrap.pypa.io/get-pip.py | python3
fi

PIP=$(command -v pip3 || command -v pip)

print_status "Upgrading pip..."
$PIP install --upgrade pip --quiet

print_status "Installing Python dependencies (including uvicorn)..."
$PIP install -r requirements.txt --quiet
print_success "Python dependencies installed"

# ── Self-check ───────────────────────────────────────────────────────────────

# Imports and mounts both backends (and hits the DB through their startup hooks),
# so a bad .env or a blocked RDS security group fails here instead of after reload.
print_status "Running gateway self-check..."
python3 test_gateway.py
print_success "Self-check passed"

# ── PM2 deployment via ecosystem.config.js ───────────────────────────────────

print_status "Starting/reloading PM2 process via ecosystem.config.js..."
pm2 reload ecosystem.config.js --update-env || pm2 start ecosystem.config.js
print_success "PM2 process started"

print_status "Saving PM2 process list..."
pm2 save
print_success "PM2 configuration saved"

print_status "Current PM2 processes:"
pm2 status

print_success "Deployment completed successfully!"
echo ""
echo "Swagger UI:  http://<host>:6644/docs"
echo ""
echo "Useful commands:"
echo "  - View logs:    pm2 logs psa-backend"
echo "  - Restart:      pm2 restart psa-backend"
echo "  - Stop:         pm2 stop psa-backend"
echo "  - Monitor:      pm2 monit"
