#!/usr/bin/env bash
# Sets up a fresh x86 Ubuntu machine as a Warrant prover and signing host:
# Docker (for reproducible guest builds and the Groth16 wrap), Rust, RISC Zero,
# Foundry; then builds the guests reproducibly and prints the image ID to deploy.
# Run as a user with sudo. Re-runnable. Not executed in this repository's
# validation environment, which has no Docker daemon.
set -euo pipefail

sudo apt-get update
sudo apt-get install -y build-essential curl git pkg-config libssl-dev ca-certificates
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER"
  echo "Docker installed; log out and in again (or run 'newgrp docker') before continuing."
fi
if ! command -v cargo >/dev/null; then
  curl -fsSL https://sh.rustup.rs | sh -s -- -y
  # shellcheck disable=SC1090
  source "$HOME/.cargo/env"
fi
if ! command -v rzup >/dev/null; then
  curl -fsSL https://risczero.com/install | bash
  export PATH="$HOME/.risc0/bin:$PATH"
fi
rzup install rust 1.88.0
rzup install r0vm 3.0.5
if ! command -v forge >/dev/null; then
  curl -fsSL https://foundry.paradigm.xyz | bash
  "$HOME/.foundry/bin/foundryup"
  export PATH="$HOME/.foundry/bin:$PATH"
fi

cd "$(dirname "$0")/.."
docker info >/dev/null
RISC0_USE_DOCKER=1 RISC0_BUILD_LOCKED=1 cargo build -p warrant-host --release --locked
echo "Reproducible invoice image ID (deploy with this): $(target/release/warrant-host invoice-image-id)"
echo "Reproducible task image ID: $(target/release/warrant-host image-id)"
