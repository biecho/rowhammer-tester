FROM ubuntu:22.04

# Prevent any interactive time zone configuration and user prompts during package installation
ENV DEBIAN_FRONTEND=noninteractive

# Update package lists and install development tools
RUN apt-get update && apt-get install -y \
    build-essential \
    autoconf \
    cmake \
    flex \
    bison \
    git \
    libftdi-dev \
    libjson-c-dev \
    libevent-dev \
    libtinfo-dev \
    uml-utilities \
    python3 \
    python3-venv \
    python3-wheel \
    protobuf-compiler \
    libcairo2-dev \
    libftdi1-2 \
    libftdi1-dev \
    libhidapi-hidraw0 \
    libhidapi-dev \
    libudev-dev \
    pkg-config \
    tree \
    zlib1g-dev \
    zip \
    unzip \
    help2man \
    curl \
    ethtool \
    # Handle specific dependency issues related to gcc and libc
    gcc-9-base \
    libtool \
    libusb-1.0-0-dev \
    && rm -rf /var/lib/apt/lists/*

CMD ["bash"]
