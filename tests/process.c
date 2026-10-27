#include <signal.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

int main(int argc, char **argv) {
    if (argc != 2)
        return 2;
    if (!strcmp(argv[1], "tree")) {
        pid_t child = fork();
        if (child < 0)
            return 3;
        if (!child) {
            for (;;)
                pause();
        }
        printf("%d\n", child);
        fflush(stdout);
        for (;;)
            pause();
    }
    printf("%d\n", getpid());
    fflush(stdout);
    if (!strcmp(argv[1], "output")) {
        char data[65536] = {0};
        for (int n = 0; n < 256; ++n) {
            size_t offset = 0;
            while (offset < sizeof(data)) {
                ssize_t count = write(1, data + offset, sizeof(data) - offset);
                if (count <= 0)
                    return 4;
                offset += (size_t)count;
            }
        }
    }
    for (;;)
        pause();
}
