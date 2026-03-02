// build with `gcc -O2 -o crial_helper crial_helper.c`
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <termios.h>
#include <errno.h>
#include <sched.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <sys/select.h>
#include <signal.h>

#define SOCKET_PATH "/tmp/serial_bridge.sock"
#define BUF_SIZE 4096
#define CMD_MARKER 0x01
#define KBD_DELAY_US 130000

static volatile int running = 1;

void cleanup(int sig) {
    running = 0;
}

int open_serial(const char *port) {
    int fd = open(port, O_RDWR | O_NOCTTY);
    if (fd < 0) {
        perror("open serial");
        return -1;
    }

    struct termios attrs;
    tcgetattr(fd, &attrs);
    attrs.c_iflag = 0x406;
    attrs.c_oflag = 0x0;
    attrs.c_cflag = 0x18b8;
    attrs.c_lflag = 0x8a30;
    cfsetispeed(&attrs, B1000000);
    cfsetospeed(&attrs, B1000000);
    attrs.c_cc[VMIN]  = 255;
    attrs.c_cc[VTIME] = 2;
    tcsetattr(fd, TCSANOW, &attrs);
    tcflush(fd, TCIFLUSH);
    return fd;
}

int main(int argc, char *argv[]) {
    if (argc < 2) {
        fprintf(stderr, "usage: serial_bridge <port>\n");
        return 1;
    }

    struct sched_param sp = { .sched_priority = 10 };
    if (sched_setscheduler(0, SCHED_FIFO, &sp) < 0) {
        fprintf(stderr, "Warning: could not set real-time priority: %s\n", strerror(errno));
    }

    signal(SIGINT,  cleanup);
    signal(SIGTERM, cleanup);
    signal(SIGPIPE, SIG_IGN);  // don't crash if client disconnects during write

    int serial_fd = open_serial(argv[1]);
    if (serial_fd < 0) return 1;

    unlink(SOCKET_PATH);
    int sock_fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (sock_fd < 0) { perror("socket"); return 1; }

    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    strncpy(addr.sun_path, SOCKET_PATH, sizeof(addr.sun_path) - 1);

    if (bind(sock_fd, (struct sockaddr*)&addr, sizeof(addr)) < 0) {
        perror("bind"); return 1;
    }
    if (listen(sock_fd, 1) < 0) {
        perror("listen"); return 1;
    }

    fprintf(stderr, "serial_bridge ready on %s\n", SOCKET_PATH);

    while (running) {
        // accept loop — reconnects if python restarts
        fprintf(stderr, "Waiting for client...\n");
        int client_fd = accept(sock_fd, NULL, NULL);
        if (client_fd < 0) {
            if (errno == EINTR) continue;
            perror("accept");
            break;
        }
        fprintf(stderr, "Client connected, bridging %s\n", argv[1]);

        char buf[BUF_SIZE];
        int maxfd = (serial_fd > client_fd ? serial_fd : client_fd) + 1;

        while (running) {
            fd_set fds;
            FD_ZERO(&fds);
            FD_SET(serial_fd, &fds);
            FD_SET(client_fd, &fds);

            if (select(maxfd, &fds, NULL, NULL, NULL) < 0) {
                if (errno == EINTR) continue;
                perror("select");
                goto next_client;
            }

            // serial -> socket
            if (FD_ISSET(serial_fd, &fds)) {
                ssize_t n = read(serial_fd, buf, BUF_SIZE);
                if (n > 0) {
                    if (send(client_fd, buf, n, MSG_NOSIGNAL) < 0) {
                        fprintf(stderr, "Client send failed\n");
                        goto next_client;
                    }
                }
            }

            // socket -> serial
            if (FD_ISSET(client_fd, &fds)) {
                ssize_t n = recv(client_fd, buf, BUF_SIZE, 0);
                if (n <= 0) {
                    fprintf(stderr, "Client disconnected\n");
                    goto next_client;
                }
                if (n > 1 && (unsigned char)buf[0] == CMD_MARKER) {
                    // char-by-char with delay
                    for (ssize_t i = 1; i < n; i++) {
                        write(serial_fd, &buf[i], 1);
                        usleep(KBD_DELAY_US);
                    }
                } else {
                    write(serial_fd, buf, n);
                }
            }
        }
        next_client:
        close(client_fd);
    }

    close(sock_fd);
    close(serial_fd);
    unlink(SOCKET_PATH);
    return 0;
}