#include <CommonCrypto/CommonDigest.h>
#import <Foundation/Foundation.h>
#include <arpa/inet.h>
#include <array>
#include <cerrno>
#include <chrono>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <poll.h>
#include <stdexcept>
#include <string>
#include <sys/stat.h>
#include <sys/time.h>
#include <sys/wait.h>
#include <unistd.h>
#include <vector>

namespace {
constexpr size_t limit = 1024 * 1024;
enum Kind { Request, Out, Err, Exit, Input, Eof, Signal, Error, Started, Window };
pid_t group = -1;
volatile sig_atomic_t timed_out = 0;
std::vector<unsigned char> outbound;
bool buffering = false;

void interrupted(int signal) {
    if (group > 0)
        kill(-group, SIGKILL);
    _exit(128 + signal);
}

void deadline_reached(int) {
    timed_out = 1;
    if (group > 0)
        kill(-group, SIGKILL);
}

void require(bool ok, const char *message) {
    if (!ok)
        throw std::runtime_error(message);
}

void write_all(int fd, const void *data, size_t size) {
    const char *p = static_cast<const char *>(data);
    while (size) {
        ssize_t n = write(fd, p, size);
        if (n < 0 && errno == EINTR)
            continue;
        require(n > 0, "stream write failed");
        p += n;
        size -= static_cast<size_t>(n);
    }
}

void read_all(int fd, void *data, size_t size) {
    char *p = static_cast<char *>(data);
    while (size) {
        ssize_t n = read(fd, p, size);
        if (n < 0 && errno == EINTR)
            continue;
        require(n > 0, "incomplete request");
        p += n;
        size -= static_cast<size_t>(n);
    }
}

void frame(unsigned char kind, const void *data, size_t size) {
    require(size <= limit, "oversized response");
    uint32_t length = htonl(static_cast<uint32_t>(size));
    if (buffering) {
        require(outbound.size() + size + 5 <= 2 * limit, "output queue exceeded");
        outbound.push_back(kind);
        const auto *p = reinterpret_cast<const unsigned char *>(&length);
        outbound.insert(outbound.end(), p, p + 4);
        p = static_cast<const unsigned char *>(data);
        outbound.insert(outbound.end(), p, p + size);
        return;
    }
    write_all(STDOUT_FILENO, &kind, 1);
    write_all(STDOUT_FILENO, &length, 4);
    write_all(STDOUT_FILENO, data, size);
}

void json_frame(unsigned char kind, NSDictionary *body) {
    NSData *data = [NSJSONSerialization dataWithJSONObject:body options:0 error:nil];
    require(data != nil, "response encoding failed");
    frame(kind, data.bytes, data.length);
}

std::string text(id value) {
    require([value isKindOfClass:[NSString class]], "expected a string");
    const char *p = [value UTF8String];
    require(p && strlen(p) == [value lengthOfBytesUsingEncoding:NSUTF8StringEncoding],
            "embedded NUL is not supported");
    return p;
}

std::string digest(int fd) {
    CC_SHA256_CTX context;
    CC_SHA256_Init(&context);
    std::array<unsigned char, 128 * 1024> buffer;
    while (true) {
        ssize_t n = read(fd, buffer.data(), buffer.size());
        if (n < 0 && errno == EINTR)
            continue;
        require(n >= 0, "file read failed");
        if (!n)
            break;
        CC_SHA256_Update(&context, buffer.data(), static_cast<CC_LONG>(n));
    }
    unsigned char hash[CC_SHA256_DIGEST_LENGTH];
    CC_SHA256_Final(hash, &context);
    std::string result;
    for (unsigned char byte : hash) {
        result += "0123456789abcdef"[byte >> 4];
        result += "0123456789abcdef"[byte & 15];
    }
    return result;
}

NSDictionary *file_info(NSString *path) {
    int fd = open(text(path).c_str(), O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
    require(fd >= 0, "cannot open regular file");
    try {
        struct stat st;
        require(fstat(fd, &st) == 0 && S_ISREG(st.st_mode), "expected a regular file");
        std::string hash = digest(fd);
        close(fd);
        return
            @{@"size" : @(st.st_size),
              @"mode" : @(st.st_mode & 0777),
              @"sha256" : @(hash.c_str())};
    } catch (...) {
        close(fd);
        throw;
    }
}

void nonblock(int fd) {
    require(fcntl(fd, F_SETFL, fcntl(fd, F_GETFL) | O_NONBLOCK) == 0, "cannot configure pipe");
}

struct Result {
    int code;
    bool incomplete;
};

Result execute(NSDictionary *request) {
    NSArray *items = request[@"argv"];
    require([items isKindOfClass:[NSArray class]] && items.count && items.count < 65536,
            "argv must contain a program");
    std::vector<std::string> storage;
    for (id item in items)
        storage.push_back(text(item));
    require(!storage[0].empty(), "program is empty");
    std::vector<char *> argv;
    for (auto &item : storage)
        argv.push_back(item.data());
    argv.push_back(nullptr);
    id requested_cwd = request[@"cwd"];
    std::string cwd = requested_cwd && requested_cwd != [NSNull null] ? text(requested_cwd)
                                                                      : text(NSHomeDirectory());
    std::string path = text(NSHomeDirectory()) +
                       "/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin";
    NSDictionary *environment = request[@"env"] ?: @{};
    require([environment isKindOfClass:[NSDictionary class]], "env must be an object");
    std::vector<std::pair<std::string, std::string>> env;
    double timeout = request[@"timeout"] ? [request[@"timeout"] doubleValue] : 0;
    require(timeout >= 0 && timeout <= 86400, "invalid command deadline");
    for (id key in environment) {
        auto name = text(key);
        require(!name.empty() && name.find('=') == std::string::npos, "invalid env key");
        env.emplace_back(name, text(environment[key]));
    }
    int in[2], out[2], err[2];
    require(pipe(in) == 0 && pipe(out) == 0 && pipe(err) == 0, "pipe failed");
    group = fork();
    require(group >= 0, "fork failed");
    if (!group) {
        if (setpgid(0, 0) || dup2(in[0], 0) < 0 || dup2(out[1], 1) < 0 || dup2(err[1], 2) < 0)
            _exit(126);
        for (int fd : {in[0], in[1], out[0], out[1], err[0], err[1]})
            close(fd);
        if (chdir(cwd.c_str())) {
            perror("macdesk: working directory");
            _exit(126);
        }
        // A stable non-login PATH; user overrides stay explicit.
        setenv("PATH", path.c_str(), 1);
        for (const auto &[key, value] : env)
            setenv(key.c_str(), value.c_str(), 1);
        execvp(argv[0], argv.data());
        perror("macdesk: exec");
        _exit(errno == ENOENT ? 127 : 126);
    }
    setpgid(group, group);
    if (timeout > 0) {
        struct itimerval timer = {};
        timer.it_value.tv_sec = static_cast<time_t>(timeout);
        timer.it_value.tv_usec =
            static_cast<suseconds_t>((timeout - timer.it_value.tv_sec) * 1000000);
        if (!timer.it_value.tv_sec && !timer.it_value.tv_usec)
            timer.it_value.tv_usec = 1;
        require(setitimer(ITIMER_REAL, &timer, nullptr) == 0, "cannot arm command deadline");
    }
    close(in[0]);
    close(out[1]);
    close(err[1]);
    for (int fd : {in[1], out[0], err[0], STDIN_FILENO, STDOUT_FILENO})
        nonblock(fd);
    buffering = true;
    json_frame(Started, @{@"pid" : @(group)});
    std::vector<unsigned char> incoming, pending;
    bool eof = false, done = false;
    bool incomplete = false;
    auto finished_at = std::chrono::steady_clock::time_point{};
    int status = 0;
    while (!done || out[0] >= 0 || err[0] >= 0 || !outbound.empty()) {
        bool readable = outbound.size() < limit / 2;
        struct pollfd fds[] = {
            {pending.size() > limit / 2 ? -1 : STDIN_FILENO, POLLIN, 0},
            {readable ? out[0] : -1, POLLIN, 0},
            {readable ? err[0] : -1, POLLIN, 0},
            {in[1], static_cast<short>(pending.empty() ? 0 : POLLOUT), 0},
            {STDOUT_FILENO, static_cast<short>(outbound.empty() ? 0 : POLLOUT), 0}};
        int n = poll(fds, 5, 100);
        if (n < 0 && errno == EINTR)
            continue;
        require(n >= 0, "poll failed");
        if (fds[4].revents & (POLLOUT | POLLERR | POLLHUP)) {
            ssize_t count = write(STDOUT_FILENO, outbound.data(), outbound.size());
            if (count > 0)
                outbound.erase(outbound.begin(), outbound.begin() + count);
            else
                require(count < 0 && (errno == EAGAIN || errno == EINTR), "response stream closed");
        }
        if (fds[0].revents & (POLLIN | POLLHUP)) {
            unsigned char buffer[65536];
            ssize_t count = read(STDIN_FILENO, buffer, sizeof(buffer));
            require(count != 0, "client disconnected; command cancelled");
            if (count > 0)
                incoming.insert(incoming.end(), buffer, buffer + count);
            else
                require(errno == EAGAIN || errno == EINTR, "input read failed");
            require(incoming.size() <= limit + 5, "input frame buffer exceeded");
            while (incoming.size() >= 5) {
                uint32_t size;
                memcpy(&size, incoming.data() + 1, 4);
                size = ntohl(size);
                require(size <= limit, "oversized input");
                if (incoming.size() < size + 5)
                    break;
                unsigned char kind = incoming[0];
                if (kind == Input) {
                    require(!eof && pending.size() + size <= 2 * limit, "stdin buffer exceeded");
                    pending.insert(pending.end(), incoming.begin() + 5,
                                   incoming.begin() + 5 + size);
                } else if (kind == Eof) {
                    require(size == 0, "invalid EOF frame");
                    eof = true;
                } else if (kind == Signal) {
                    require(size == 1 && (incoming[5] == SIGINT || incoming[5] == SIGTERM ||
                                          incoming[5] == SIGKILL),
                            "unsupported signal");
                    kill(-group, incoming[5]);
                } else
                    throw std::runtime_error("unexpected input frame");
                incoming.erase(incoming.begin(), incoming.begin() + 5 + size);
            }
        }
        for (int index = 1; index <= 2; ++index) {
            int &fd = index == 1 ? out[0] : err[0];
            if (fds[index].revents & (POLLIN | POLLHUP)) {
                unsigned char buffer[65536];
                ssize_t count = read(fd, buffer, sizeof(buffer));
                if (count > 0)
                    frame(index == 1 ? Out : Err, buffer, count);
                else if (!count) {
                    close(fd);
                    fd = -1;
                } else
                    require(errno == EAGAIN || errno == EINTR, "output read failed");
            }
        }
        if (in[1] >= 0 && !pending.empty() && fds[3].revents & (POLLOUT | POLLERR)) {
            ssize_t count = write(in[1], pending.data(), pending.size());
            if (count > 0) {
                pending.erase(pending.begin(), pending.begin() + count);
                uint32_t accepted = htonl(static_cast<uint32_t>(count));
                frame(Window, &accepted, 4);
            } else if (count < 0 && errno == EPIPE) {
                close(in[1]);
                in[1] = -1;
                uint32_t accepted = htonl(static_cast<uint32_t>(pending.size()));
                frame(Window, &accepted, 4);
                pending.clear();
            } else
                require(errno == EAGAIN || errno == EINTR, "stdin write failed");
        }
        if (in[1] >= 0 && eof && pending.empty()) {
            close(in[1]);
            in[1] = -1;
        }
        if (!done) {
            pid_t waited = waitpid(group, &status, WNOHANG);
            if (waited == group) {
                done = true;
                finished_at = std::chrono::steady_clock::now();
                struct itimerval timer = {};
                setitimer(ITIMER_REAL, &timer, nullptr);
                // Ordinary descendants may still hold pipes: end the owned group.
                kill(-group, SIGKILL);
                if (in[1] >= 0) {
                    close(in[1]);
                    in[1] = -1;
                }
                pending.clear();
            } else
                require(waited >= 0 || errno == EINTR, "waitpid failed");
        }
        if (done && outbound.empty() &&
            std::chrono::steady_clock::now() - finished_at > std::chrono::seconds(2)) {
            for (int *fd : {&out[0], &err[0]}) {
                if (*fd >= 0) {
                    close(*fd);
                    *fd = -1;
                    incomplete = true;
                }
            }
        }
    }
    group = -1;
    buffering = false;
    fcntl(STDOUT_FILENO, F_SETFL, fcntl(STDOUT_FILENO, F_GETFL) & ~O_NONBLOCK);
    return {timed_out ? 124 : (WIFEXITED(status) ? WEXITSTATUS(status) : 128 + WTERMSIG(status)),
            incomplete};
}
} // namespace

int main() {
    @autoreleasepool {
        signal(SIGPIPE, SIG_IGN);
        signal(SIGHUP, interrupted);
        signal(SIGTERM, interrupted);
        signal(SIGINT, interrupted);
        signal(SIGALRM, deadline_reached);
        try {
            unsigned char kind;
            uint32_t length;
            read_all(0, &kind, 1);
            read_all(0, &length, 4);
            length = ntohl(length);
            require(kind == Request && length <= 256 * 1024, "invalid request header");
            NSMutableData *data = [NSMutableData dataWithLength:length];
            read_all(0, data.mutableBytes, length);
            id object = [NSJSONSerialization JSONObjectWithData:data options:0 error:nil];
            require([object isKindOfClass:[NSDictionary class]], "request must be an object");
            NSDictionary *request = object;
            NSString *op = request[@"op"];
            if ([op isEqual:@"run"]) {
                Result result = execute(request);
                json_frame(
                    Exit,
                    @{@"code" : @(result.code),
                      @"output_incomplete" : @(result.incomplete)});
            } else if ([op isEqual:@"info"]) {
                json_frame(
                    Exit, @{
                        @"home" : NSHomeDirectory(),
                        @"uid" : @(getuid()),
                        @"gid" : @(getgid()),
                        @"protocol" : @1
                    });
            } else if ([op isEqual:@"stat"]) {
                json_frame(Exit, file_info(request[@"path"]));
            } else if ([op isEqual:@"commit"]) {
                std::string source = text(request[@"stage"]), dest = text(request[@"path"]);
                require(source.substr(0, source.rfind('/')) == dest.substr(0, dest.rfind('/')),
                        "stage must be beside destination");
                NSDictionary *info = file_info(request[@"stage"]);
                require([info[@"sha256"] isEqual:request[@"sha256"]], "upload digest mismatch");
                int mode = [request[@"mode"] intValue] & 0777;
                int fd = open(source.c_str(), O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
                require(fd >= 0, "cannot open upload for commit");
                int durable = fchmod(fd, mode) || fsync(fd);
                close(fd);
                require(durable == 0, "cannot prepare uploaded file for commit");
                int renamed = [request[@"replace"] boolValue]
                                  ? rename(source.c_str(), dest.c_str())
                                  : renamex_np(source.c_str(), dest.c_str(), RENAME_EXCL);
                require(renamed == 0, "cannot commit upload (destination may already exist)");
                NSMutableDictionary *result = [info mutableCopy];
                result[@"mode"] = @(mode);
                json_frame(Exit, result);
            } else
                throw std::runtime_error("unknown operation");
            return 0;
        } catch (const std::exception &error) {
            if (group > 0) {
                kill(-group, SIGKILL);
                while (waitpid(group, nullptr, 0) < 0 && errno == EINTR) {
                }
            }
            try {
                buffering = false;
                fcntl(STDOUT_FILENO, F_SETFL, fcntl(STDOUT_FILENO, F_GETFL) & ~O_NONBLOCK);
                if (!outbound.empty())
                    write_all(STDOUT_FILENO, outbound.data(), outbound.size());
                json_frame(Error, @{@"error" : @(error.what())});
            } catch (...) {
            }
            return 1;
        }
    }
}
