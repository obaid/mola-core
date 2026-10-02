# SSH and action shells connect to the same graphical session as the desktop.
export DISPLAY=:1 LIBGL_ALWAYS_SOFTWARE=1
if [ "$(id -u)" = 1000 ]; then
    export XDG_RUNTIME_DIR=/run/user/1000
    export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus
fi
