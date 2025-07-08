import math

import psutil

def is_lfs_running():
    for proc in psutil.process_iter():
        try:
            if proc.name() == "LFS.exe":
                print("LFS.exe seems to be running. Starting!\n\n")

                return True

        except psutil.AccessDenied:
            print(
                "It seems like you do not have sufficient permissions to check for System Apps. Cannot automatically detect if LFS is running!")
            return True

    return False


def is_spotify_running():
    for proc in psutil.process_iter():
        try:
            if proc.name() == "Spotify.exe":
                return True

        except psutil.AccessDenied:
            print(
                "It seems like you do not have sufficient permissions to check for System Apps. Cannot automatically detect if LFS is running!")
            return True

    return False

def calc_polygon_points(own_x, own_y, length, angle):
    return own_x + length * math.cos(math.radians(angle)), own_y + length * math.sin(math.radians(angle))