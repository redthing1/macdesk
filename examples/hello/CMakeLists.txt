cmake_minimum_required(VERSION 3.24)
project(hello LANGUAGES CXX)

add_executable(hello main.cpp)
target_compile_features(hello PRIVATE cxx_std_20)
target_compile_options(hello PRIVATE -Wall -Wextra -Werror)

enable_testing()
add_test(NAME hello COMMAND hello)
