#include <cstdio>

#if !defined(__APPLE__) || !defined(__arm64__)
#error This example must be built for ARM64 macOS.
#endif

int main() { std::puts("Hello from ARM64 macOS"); }
