# pkgsender — command line (Linux / macOS)

`pkgsender.py` is the command-line counterpart of the Windows PKG Sender
GUI, for people who do not run Windows. Same receiver, same protocol: the
console is handed an `http://` URL and pulls the package from a small
range-capable file server that the tool starts on your machine.

Python 3.8+, standard library only. No `pip install`, no build step.

```sh
chmod +x cli/pkgsender.py
./cli/pkgsender.py discover
```

Put it on your `PATH` if you like:

```sh
ln -s "$PWD/cli/pkgsender.py" /usr/local/bin/pkgsender
```

## Setup

1. Jailbreak the console and run `payload/pkg-receiver.elf` on it. Wait
   for the toast *listening on port 12800*.
2. Put the console and this machine on the same network.
3. `pkgsender discover --save` — remembers the console address in
   `~/.config/pkg-sender/cli.json`, so later commands need no `--ps`.

**macOS firewall:** the console connects *back* to this machine on TCP
9898, so allow incoming connections for `python3` when macOS asks (System
Settings → Network → Firewall → Options). Without it the transfer never
starts and `send` reports *the console never asked for the file*.

## Commands

| Command | What it does |
| --- | --- |
| `discover` | Finds consoles: UDP beacon first, LAN sweep as fallback |
| `status` | Receiver build, install state, pull-copy progress |
| `send` | Installs PKGs: serves them, pushes them, tracks download + install |
| `copy` | Writes files/folders to the console filesystem (e.g. the payload) |
| `serve` | Only serves the files and prints their URLs |
| `info` | Reads PKG metadata (title, title id, version, cover) |
| `config` | Shows or changes the saved defaults |

### send

```sh
pkgsender send ~/pkgs/game.pkg                 # one package
pkgsender send ~/pkgs                          # every .pkg in a folder
pkgsender send --ps 192.168.1.105 a.pkg b.pkg  # explicit console
pkgsender send --dry-run ~/pkgs                # show what would be sent
```

Packages go one at a time: push → wait for the console to download every
byte → wait for `/api/status` to go from busy back to idle (that is the
install finishing) → next package. The package title and cover art are
read from the PKG and shown on the console's installer UI.

**Keep the process running** until it prints *Finished* — it is the file
server the console is downloading from. Ctrl-C revokes the URLs, so an
in-flight download fails on the console instead of finishing silently.

Useful flags: `--pc IP` (address the console should pull from, when
auto-detection picks the wrong interface), `--port` (file-server port,
default 9898), `--no-wait` (stop tracking once the download finishes),
`--pull-timeout SEC` (give up if the console never starts downloading).

### copy

```sh
pkgsender copy payload/pkg-receiver.elf /data/homebrew
pkgsender copy ~/homebrew-app                     # folder, structure kept
pkgsender copy --dest /data/homebrew a.bin b.bin
```

Chunked upload with resume (it asks the console what it already has),
retry with backoff, skip when the console already holds a file of the
same size (`--force` re-sends), and a size check after every file.

A trailing path that does not exist locally is taken as the remote
directory; `--dest` says it explicitly. Default: `/data/homebrew`.

### Other

```sh
pkgsender status --json          # machine-readable state
pkgsender info ~/pkgs/game.pkg   # title, title id, version, platform
pkgsender serve ~/pkgs/*.pkg     # print URLs, install from the console UI
pkgsender config --ps-ip 192.168.1.105
```

`--ps` beats `$PKGSENDER_PS`, which beats the config file, which beats
auto-discovery. Exit status is 0 on success, 1 on failure, 130 on Ctrl-C.

## Testing without a console

`tests/mock_receiver.py` stands in for the payload: it answers the file
endpoints and really downloads a pushed URL with range requests, then
reports itself busy for a moment so the install gate is exercised.

```sh
python3 tests/mock_receiver.py 12800 &
pkgsender send --ps 127.0.0.1 --pc 127.0.0.1 ~/pkgs/game.pkg
pkgsender copy --ps 127.0.0.1 ~/pkgs/game.pkg /data/homebrew
```

## Differences from the Windows GUI

The CLI covers discovery, installing and copying. The library browser,
family linking (patches/DLC grouped under their base game), non-PKG
formats (`.exfat`, `.ffpkg`, `.ffpfsc`), the console-side catalog and the
self-updater stay GUI-only.
