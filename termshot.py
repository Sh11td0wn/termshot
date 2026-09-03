#!/usr/bin/env python3
"""
termshot.py — Executa um comando no shell e gera uma imagem (PNG/SVG)
que reproduz visualmente a saída, como um "printscreen" de terminal.

Uso básico:
    python3 termshot.py "ls -la"
    python3 termshot.py "nmap -sV 192.168.1.1" -o resultado.png
    python3 termshot.py "cat /etc/os-release" --title "Sistema"

Como funciona:
    1. Executa o comando dentro de um pseudo-terminal (pty), para que
       ferramentas que só colorem a saída quando detectam um TTY real
       (grep, ls, nmap, etc.) mantenham suas cores ANSI.
    2. Captura stdout+stderr combinados (como você veria na tela).
    3. Usa a lib `rich` para interpretar os códigos ANSI e renderizar
       tudo num "console" virtual.
    4. Exporta esse console como imagem (PNG via SVG, ou SVG direto).
"""

import argparse
import fcntl
import io
import os
import pty
import re
import select
import shlex
import shutil
import struct
import subprocess
import sys
import termios
import time
from datetime import datetime

from rich.console import Console
from rich.terminal_theme import TerminalTheme
from rich.text import Text


__version__ = "1.0"


CSI_RE = re.compile(r'\x1b\[([?0-9;:>]*)([ -/]?)([@-~])')

# Tema customizado para exportação: fundo 100% preto e primeiro-plano
# em branco puro (mais forte que o cinza-claro padrão do rich), mantendo
# a paleta de 16 cores ANSI padrão para que a saída colorida de comandos
# (ls, grep, nmap, etc.) continue sendo exibida corretamente.
BLACK_BG_THEME = TerminalTheme(
    (0, 0, 0),        # background
    (255, 255, 255),  # foreground
    [
        (0, 0, 0),        # black
        (205, 49, 49),    # red
        (13, 188, 121),   # green
        (229, 229, 16),   # yellow
        (36, 114, 200),   # blue
        (188, 63, 188),   # magenta
        (17, 168, 205),   # cyan
        (229, 229, 229),  # white
    ],
    [
        (102, 102, 102),  # bright black
        (241, 76, 76),    # bright red
        (35, 209, 139),   # bright green
        (245, 245, 67),   # bright yellow
        (59, 142, 234),   # bright blue
        (214, 112, 214),  # bright magenta
        (41, 184, 219),   # bright cyan
        (255, 255, 255),  # bright white
    ],
)


def run_command_capturing_ansi(
    command: str,
    timeout: int | None = None,
    width: int = 120,
    height: int = 40,
    live_output: bool = True,
) -> tuple[str, int]:
    """
    Executa `command` dentro de um PTY, capturando a saída combinada
    (stdout+stderr) com os códigos ANSI intactos. Por padrão, cada bloco
    recebido também é reproduzido imediatamente no terminal atual.

    Retorna (saida_texto, codigo_de_saida).
    """
    master_fd, slave_fd = pty.openpty()

    # Informa às aplicações o tamanho real do terminal virtual. Isso é
    # importante para bibliotecas de progresso (Rich, tqdm, etc.), que usam
    # a largura/altura do TTY para redesenhar a mesma região da tela.
    fcntl.ioctl(
        slave_fd,
        termios.TIOCSWINSZ,
        struct.pack("HHHH", height, width, 0, 0),
    )

    proc = subprocess.Popen(
        command,
        shell=True,
        executable="/bin/bash",
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        close_fds=True,
        env={**os.environ, "TERM": "xterm-256color"},
    )
    os.close(slave_fd)

    output_chunks = []
    start = time.time()

    def capture(data: bytes) -> None:
        """Armazena a saída e, quando habilitado, também a exibe ao vivo."""
        output_chunks.append(data)
        if live_output:
            try:
                os.write(sys.stdout.fileno(), data)
            except (AttributeError, OSError, ValueError):
                # Fallback para ambientes em que stdout não expõe fileno().
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()

    try:
        while True:
            if proc.poll() is not None:
                # processo terminou; ainda drena o que sobrou no buffer
                while True:
                    r, _, _ = select.select([master_fd], [], [], 0.1)
                    if not r:
                        break
                    try:
                        data = os.read(master_fd, 65536)
                    except OSError:
                        break
                    if not data:
                        break
                    capture(data)
                break

            if timeout is not None and time.time() - start > timeout:
                proc.kill()
                proc.wait()
                capture(b"\n[termshot] TIMEOUT: comando excedeu o limite de tempo.\n")
                break

            r, _, _ = select.select([master_fd], [], [], 0.2)
            if r:
                try:
                    data = os.read(master_fd, 65536)
                except OSError:
                    break
                if not data:
                    break
                capture(data)
    finally:
        os.close(master_fd)

    # Garante que o processo terminou e o returncode foi coletado
    proc.wait()

    raw = b"".join(output_chunks)
    text = raw.decode("utf-8", errors="replace")
    return text, proc.returncode


def clean_terminal_control_sequences(text: str, width: int = 120) -> str:
    """
    Emula a parte do comportamento de um terminal necessária para saídas
    dinâmicas: retorno de carro, backspace, movimentação do cursor e limpeza
    de linha/tela. O resultado representa o estado FINAL da tela, em vez de
    concatenar todos os quadros intermediários de barras de progresso.

    As sequências SGR (cores/estilos) são preservadas por caractere para que
    o Rich ainda consiga reproduzir a aparência original na imagem.
    """
    # Cada célula contém (caractere, sequência SGR ativa).
    rows: list[list[tuple[str, str]]] = [[]]
    row = col = 0
    saved_cursor = (0, 0)
    style = ""

    def ensure_row(index: int) -> None:
        while len(rows) <= index:
            rows.append([])

    def put(ch: str) -> None:
        nonlocal row, col
        ensure_row(row)
        line = rows[row]
        while len(line) < col:
            line.append((" ", ""))
        cell = (ch, style)
        if col < len(line):
            line[col] = cell
        else:
            line.append(cell)
        col += 1
        if col >= width:
            row += 1
            col = 0
            ensure_row(row)

    def param(values: str, default: int = 1, position: int = 0) -> int:
        clean = values.lstrip("?")
        parts = clean.split(";") if clean else []
        if position >= len(parts) or parts[position] == "":
            return default
        try:
            return int(parts[position].split(":", 1)[0])
        except ValueError:
            return default

    i = 0
    while i < len(text):
        ch = text[i]

        if ch == "\x1b":
            match = CSI_RE.match(text, i)
            if match:
                values, _, command = match.groups()
                full = match.group(0)
                if command == "m":
                    codes = values.split(";") if values else ["0"]
                    # Um reset invalida os estilos acumulados anteriores.
                    if "0" in codes or values == "":
                        style = "" if codes == ["0"] else full
                    else:
                        style += full
                elif command == "A":
                    row = max(0, row - param(values))
                elif command in ("B", "e"):
                    row += param(values)
                    ensure_row(row)
                elif command in ("C", "a"):
                    col = min(width - 1, col + param(values))
                elif command == "D":
                    col = max(0, col - param(values))
                elif command in ("G", "`"):
                    col = max(0, param(values) - 1)
                elif command in ("H", "f"):
                    row = max(0, param(values, 1, 0) - 1)
                    col = max(0, param(values, 1, 1) - 1)
                    ensure_row(row)
                elif command == "K":
                    ensure_row(row)
                    mode = param(values, 0)
                    if mode == 0:
                        del rows[row][col:]
                    elif mode == 1:
                        end = min(col + 1, len(rows[row]))
                        rows[row][:end] = [(" ", "")] * end
                    elif mode == 2:
                        rows[row] = []
                        col = 0
                elif command == "J":
                    mode = param(values, 0)
                    if mode in (2, 3):
                        rows = [[]]
                        row = col = 0
                    elif mode == 0:
                        ensure_row(row)
                        del rows[row][col:]
                        del rows[row + 1:]
                elif command == "s":
                    saved_cursor = (row, col)
                elif command == "u":
                    row, col = saved_cursor
                    ensure_row(row)
                i = match.end()
                continue

            # OSC (título, hyperlink etc.): ignora até BEL ou ST.
            if i + 1 < len(text) and text[i + 1] == "]":
                bel = text.find("\x07", i + 2)
                st = text.find("\x1b\\", i + 2)
                ends = [p for p in (bel, st) if p != -1]
                i = (min(ends) + (2 if min(ends) == st else 1)) if ends else len(text)
                continue
            i += 1
            continue

        if ch == "\r":
            col = 0
        elif ch == "\n":
            row += 1
            col = 0
            ensure_row(row)
        elif ch == "\b":
            col = max(0, col - 1)
        elif ch == "\t":
            next_tab = min(width, ((col // 8) + 1) * 8)
            while col < next_tab:
                put(" ")
        elif ch >= " " and ch != "\x7f":
            put(ch)
        i += 1

    # Reconstrói ANSI somente nas mudanças de estilo e remove espaços/linhas
    # vazias no fim, tal como a tela real aparenta após o comando terminar.
    rendered: list[str] = []
    for line in rows:
        while line and line[-1][0] == " ":
            line.pop()
        parts: list[str] = []
        active = ""
        for char, char_style in line:
            if char_style != active:
                parts.append("\x1b[0m")
                if char_style:
                    parts.append(char_style)
                active = char_style
            parts.append(char)
        if active:
            parts.append("\x1b[0m")
        rendered.append("".join(parts))

    while rendered and not rendered[-1]:
        rendered.pop()
    return "\n".join(rendered)


def truncate_output(text: str, max_lines: int, show_tail: bool = False) -> tuple[str, int]:
    """
    Trunca `text` para no máximo `max_lines` linhas, preservando os
    códigos ANSI de cada linha (o corte acontece nas quebras de linha,
    nunca no meio de uma sequência de escape).

    Por padrão (show_tail=False), mostra apenas as primeiras `max_lines`
    linhas, com um aviso ao final indicando quantas foram omitidas.

    Se show_tail=True, reserva metade do limite para as primeiras linhas
    e metade para as últimas, com o aviso entre elas.

    Retorna (texto_truncado, linhas_omitidas). linhas_omitidas é 0 se
    nada precisou ser cortado.
    """
    lines = text.split("\n")
    total = len(lines)

    if total <= max_lines:
        return text, 0

    # Cor dourada (#D7BA7D) via ANSI truecolor, para diferenciar visualmente
    # o aviso da saída real do comando.
    # \x1b[0m reseta qualquer estilo ANSI que possa ter ficado "aberto"
    # ao cortar no meio da saída, evitando que a cor vaze para o aviso
    # ou para as linhas seguintes.
    reset = "\x1b[0m"

    def colorize(s: str) -> str:
        return f"\x1b[38;2;215;186;125m{s}\x1b[0m"

    if show_tail:
        head_count = max_lines // 2
        tail_count = max_lines - head_count
        omitted = total - head_count - tail_count

        head_lines = lines[:head_count]
        tail_lines = lines[total - tail_count:]

        notice = f"⋯ {omitted} linha(s) omitida(s) ⋯"
        truncated = (
            reset + "\n".join(head_lines) + "\n"
            + colorize(notice) + "\n"
            + reset + "\n".join(tail_lines)
        )
    else:
        head_lines = lines[:max_lines]
        omitted = total - max_lines

        notice = f"⋯ {omitted} linha(s) omitida(s) ⋯"
        truncated = (
            reset + "\n".join(head_lines) + "\n"
            + colorize(notice)
        )

    return truncated, omitted


def build_terminal_image(
    command: str,
    output_text: str,
    exit_code: int,
    out_path: str,
    title: str | None = None,
    theme: str = "monokai",
    width: int = 120,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    show_timestamp: bool = True,
    prompt: str | None = None,
    show_command: bool = True,
    max_lines: int | None = 30,
    show_tail: bool = False,
):
    """
    Monta a imagem final estilo terminal usando Rich. Opcionalmente inclui
    um prompt personalizado e a linha do comando executado, e exporta para
    PNG (via CairoSVG) ou SVG.
    """
    # O Console é usado apenas como superfície de renderização para o SVG.
    # Sem um arquivo interno, console.print() também escreve no stdout real,
    # repetindo ao final toda a saída que já foi exibida ao vivo.
    render_buffer = io.StringIO()
    console = Console(
        record=True,
        width=width,
        force_terminal=True,
        color_system="truecolor",
        file=render_buffer,
    )

    if show_timestamp and started_at is not None:
        duration = None
        if finished_at is not None:
            duration = (finished_at - started_at).total_seconds()

        ts_text = Text()
        ts_text.append("[", style="#D7BA7D")
        ts_text.append(started_at.strftime("%d/%m/%Y %H:%M"), style="#D7BA7D")
        if duration is not None:
            ts_text.append(f" · duração: {duration:.2f}s", style="#D7BA7D")
        ts_text.append("]", style="#D7BA7D")
        console.print(ts_text)

    # O prompt personalizado é uma linha independente: fica depois do
    # timestamp e antes do comando. Também aceita cores ANSI.
    if prompt is not None:
        console.print(Text.from_ansi(prompt, style="bold #FFFFFF"))

    # Mostrar o comando é o comportamento padrão. --no-command oculta
    # somente esta linha, sem afetar a execução ou a saída capturada.
    if show_command:
        command_line = Text()
        command_line.append("# ", style="bold #FFFFFF")
        command_line.append(command, style="bold #FFFFFF")
        console.print(command_line)

    cleaned = clean_terminal_control_sequences(output_text, width=width)

    omitted = 0
    if max_lines is not None and max_lines > 0:
        cleaned, omitted = truncate_output(cleaned, max_lines, show_tail=show_tail)

    if cleaned.strip():
        console.print(Text.from_ansi(cleaned, style="#FFFFFF"))

    # A barra de título só aparece se o usuário explicitamente passar --title.
    # O rich sempre desenha a barra da janela; quando não há título, usamos
    # uma string vazia para que a barra fique em branco (sem texto).
    tmp_svg_path = out_path + ".tmp.svg"
    console.save_svg(tmp_svg_path, title=title or "", theme=BLACK_BG_THEME)

    with open(tmp_svg_path, "r", encoding="utf-8") as f:
        svg_content = f.read()
    svg_content = strip_window_controls(svg_content)
    with open(tmp_svg_path, "w", encoding="utf-8") as f:
        f.write(svg_content)

    return tmp_svg_path, omitted


def strip_window_controls(svg_text: str) -> str:
    """
    Remove os três círculos coloridos (estilo semáforo macOS) que o rich
    desenha no canto superior esquerdo da janela do terminal exportado.
    """
    pattern = re.compile(
        r'<g transform="translate\([^)]*\)">\s*'
        r'(?:<circle[^/]*/>\s*){3}'
        r'</g>\s*',
        re.DOTALL,
    )
    return pattern.sub("", svg_text)


def svg_to_png(svg_path: str, png_path: str):
    """Converte o SVG gerado pelo rich em PNG usando cairosvg."""
    import cairosvg
    cairosvg.svg2png(url=svg_path, write_to=png_path, scale=2.0)


def process_single_command(
    command: str,
    out_path: str,
    title: str | None,
    width: int,
    timeout: int | None,
    show_timestamp: bool,
    prompt: str | None,
    show_command: bool,
    max_lines: int | None,
    show_tail: bool,
    as_svg: bool,
    quiet: bool,
    verbose: bool,
) -> str:
    """
    Executa um único comando e gera sua imagem. Retorna o caminho final
    do arquivo gerado (.png ou .svg). Função compartilhada entre o modo
    normal (um comando) e o modo batch (vários comandos).
    """
    if verbose and not quiet:
        print(f"[termshot] Executando: {command}", flush=True)
    started_at = datetime.now()
    output_text, exit_code = run_command_capturing_ansi(
        command, timeout=timeout, width=width, live_output=not quiet
    )
    finished_at = datetime.now()
    if verbose and not quiet:
        # Evita que a mensagem do termshot fique colada a uma saída sem \n.
        if output_text and not output_text.endswith(("\n", "\r")):
            print()
        print(f"[termshot] Comando finalizado (exit code {exit_code}). Gerando imagem...")

    svg_path, omitted = build_terminal_image(
        command=command,
        output_text=output_text,
        exit_code=exit_code,
        out_path=out_path,
        title=title,
        width=width,
        started_at=started_at,
        finished_at=finished_at,
        show_timestamp=show_timestamp,
        prompt=prompt,
        show_command=show_command,
        max_lines=max_lines,
        show_tail=show_tail,
    )

    if omitted > 0 and verbose and not quiet:
        print(f"[termshot] Aviso: saída tinha mais linhas que o limite configurado; "
              f"{omitted} linha(s) foram omitidas na imagem (use --max-lines para ajustar).")

    if as_svg:
        final_path = out_path if out_path.endswith(".svg") else out_path.rsplit(".", 1)[0] + ".svg"
        os.replace(svg_path, final_path)
    else:
        final_path = out_path if out_path.endswith(".png") else out_path + ".png"
        svg_to_png(svg_path, final_path)
        os.remove(svg_path)

    if verbose and not quiet:
        print(f"[termshot] Imagem salva em: {final_path}")
    return final_path


def load_batch_commands(batch_file: str) -> list[str]:
    """
    Lê comandos de um arquivo, um por linha. Ignora linhas em branco e
    linhas iniciadas com '#' (comentários).
    """
    commands = []
    with open(batch_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            commands.append(line)
    return commands


def main():
    parser = argparse.ArgumentParser(
        description="Executa um comando (ou vários, em modo batch) e gera imagem(ns) "
                    "estilo terminal com o resultado."
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument(
        "command", nargs="?", default=None,
        help="Comando a ser executado (entre aspas). Omitido quando se usa --batch-file."
    )
    parser.add_argument(
        "-o", "--output", default=None,
        help="Caminho do arquivo de saída (padrão: termshot_<timestamp>.png). "
             "No modo batch, usado como diretório de saída."
    )
    parser.add_argument(
        "--svg", action="store_true",
        help="Gera também/apenas o arquivo .svg (vetorial) em vez de só PNG"
    )
    parser.add_argument("--title", default=None, help="Título mostrado na janela da imagem")
    parser.add_argument(
        "--width", type=int, default=None,
        help="Largura em colunas. Por padrão, detecta automaticamente a "
             "largura do terminal atual (fallback: 120)"
    )
    parser.add_argument(
        "--timeout", type=int, default=None,
        help="Timeout opcional em segundos. Por padrão, não há limite de tempo"
    )
    parser.add_argument(
        "--no-timestamp", action="store_true",
        help="Não incluir data/hora e duração da execução na imagem"
    )
    parser.add_argument(
        "--prompt", default=None,
        help="Texto de prompt exibido após o timestamp e antes do comando"
    )
    parser.add_argument(
        "--no-command", action="store_true",
        help="Não mostrar na imagem a linha com o comando executado"
    )
    parser.add_argument(
        "-q", "--quiet", action="store_true",
        help="Não exibir a saída do comando nem mensagens do Termshot; "
             "a imagem ainda é gerada normalmente"
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Além da saída do comando, mostrar mensagens de controle do Termshot"
    )
    parser.add_argument(
        "--max-lines", type=int, default=30,
        help="Máximo de linhas exibidas na imagem antes de truncar "
             "(por padrão mostra só o início, com aviso ao final). "
             "Use 0 para desativar. Padrão: 30"
    )
    parser.add_argument(
        "--show-tail", action="store_true",
        help="Ao truncar, também mostra as últimas linhas da saída "
             "(metade do limite no início, metade no fim, aviso no meio)"
    )
    parser.add_argument(
        "--batch-file", default=None,
        help="Caminho para um arquivo de texto com um comando por linha "
             "(linhas em branco e iniciadas com # são ignoradas). "
             "Gera uma imagem por comando."
    )

    args = parser.parse_args()

    if not args.batch_file and not args.command:
        parser.error("informe um comando ou use --batch-file")

    if args.quiet and args.verbose:
        parser.error("--quiet e --verbose não podem ser usados juntos")

    if args.timeout is not None and args.timeout <= 0:
        parser.error("--timeout deve ser maior que zero")

    detected_width = shutil.get_terminal_size(fallback=(120, 40)).columns
    terminal_width = args.width if args.width is not None else detected_width
    if terminal_width < 20:
        parser.error("--width deve ser igual ou maior que 20")

    if args.width is None and args.verbose and not args.quiet:
        print(f"[termshot] Largura detectada do terminal: {terminal_width} colunas")

    common_kwargs = dict(
        title=args.title,
        width=terminal_width,
        timeout=args.timeout,
        show_timestamp=not args.no_timestamp,
        prompt=args.prompt,
        show_command=not args.no_command,
        max_lines=args.max_lines,
        show_tail=args.show_tail,
        as_svg=args.svg,
        quiet=args.quiet,
        verbose=args.verbose,
    )

    if args.batch_file:
        commands = load_batch_commands(args.batch_file)
        if not commands:
            if args.verbose and not args.quiet:
                print(f"[termshot] Nenhum comando encontrado em {args.batch_file}.")
            return

        out_dir = args.output or "termshot_batch"
        os.makedirs(out_dir, exist_ok=True)
        if args.verbose and not args.quiet:
            print(f"[termshot] Modo batch: {len(commands)} comando(s) encontrados. "
                  f"Salvando em: {out_dir}/")

        generated = []
        for i, cmd in enumerate(commands, start=1):
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"termshot_{ts}_{i:02d}.png"
            out_path = os.path.join(out_dir, filename)
            if args.verbose and not args.quiet:
                print(f"\n[termshot] [{i}/{len(commands)}] {cmd}")
            final_path = process_single_command(cmd, out_path, **common_kwargs)
            generated.append(final_path)

        if args.verbose and not args.quiet:
            print(f"\n[termshot] Concluído: {len(generated)} imagem(ns) geradas em {out_dir}/")
    else:
        if args.output:
            base_out = args.output
        else:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            base_out = f"termshot_{ts}.png"

        process_single_command(args.command, base_out, **common_kwargs)


if __name__ == "__main__":
    main()
