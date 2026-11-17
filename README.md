# Termshot

Capture a saída de comandos como imagens de terminal limpas, preservando cores ANSI e tratando corretamente barras de progresso e outras atualizações dinâmicas.

O Termshot executa o comando em um pseudo-terminal (PTY), exibe sua saída em tempo real e, ao término, exporta o estado final para PNG ou SVG.

## Principais recursos

- Captura combinada de `stdout` e `stderr`.
- Preservação de cores e estilos ANSI.
- Visualização da saída em tempo real.
- Tratamento de retorno de carro, limpeza de linha e movimentação do cursor.
- Remoção dos quadros intermediários de barras de progresso.
- Detecção automática da largura atual do terminal.
- Exportação para PNG ou SVG.
- Timestamp e duração da execução.
- Prompt, título e largura personalizáveis.
- Limitação opcional do número de linhas na imagem.
- Execução de vários comandos em lote.
- Modos normal, verboso e silencioso.
- Timeout opcional, sem limite de tempo por padrão.

## Requisitos

- Linux ou outro sistema compatível com PTY, `fcntl` e `termios`.
- Python 3.10 ou superior.
- [Rich](https://github.com/Textualize/rich).
- [CairoSVG](https://cairosvg.org/).

> O Windows nativo não é suportado atualmente. No Windows, utilize WSL.

## Instalação


```bash
git clone https://github.com/Sh11td0wn/termshot.git
cd termshot
pip install -r requirements.txt
```


## Uso rápido

```bash
termshot "nmap -sV 192.168.1.10"
```

Para consultar a versão instalada:

```bash
termshot --version
```

Por padrão, o programa:

1. detecta a largura atual do terminal;
2. mostra a saída do comando em tempo real;
3. aguarda a conclusão sem aplicar timeout;
4. gera um arquivo PNG com timestamp no nome;
5. inclui na imagem o horário, a duração e o comando executado;
6. limita a imagem às primeiras 30 linhas, caso a saída seja maior.

Para definir o nome do arquivo:

```bash
termshot "nmap -sV 192.168.1.10" -o evidencias/nmap.png
```

## Exemplos

### Prompt personalizado

O prompt é exibido depois do timestamp e antes da linha do comando:

```bash
termshot "nxc smb 10.0.0.0/24" \
  --prompt "root@kali:~/cliente" \
  -o smb.png
```

### Ocultar o comando na imagem

```bash
termshot "cat arquivo.txt" --no-command -o resultado.png
```

O comando continua sendo executado normalmente; apenas sua linha é removida da imagem.

### Remover o timestamp

```bash
termshot "whoami" --no-timestamp -o whoami.png
```

### Definir uma largura manual

A largura atual é detectada automaticamente. Para substituí-la:

```bash
termshot "nmap -sV 192.168.1.10" --width 160 -o nmap.png
```

### Definir um timeout

Não há timeout por padrão. Para limitar a execução a 300 segundos:

```bash
termshot "comando demorado" --timeout 300 -o resultado.png
```

### Gerar SVG

```bash
termshot "ls -lah" --svg -o arquivos.svg
```

### Controlar o tamanho da imagem

Mostrar até 100 linhas:

```bash
termshot "comando" --max-lines 100 -o resultado.png
```

Desativar o limite:

```bash
termshot "comando" --max-lines 0 -o resultado.png
```

Dividir o limite entre o início e o final da saída:

```bash
termshot "comando" --max-lines 40 --show-tail -o resultado.png
```

### Modo verboso

A saída do comando é sempre mostrada no modo normal. Use `--verbose` para exibir também as mensagens de controle do Termshot:

```bash
termshot "nmap -sV 192.168.1.10" --verbose -o nmap.png
```

Forma abreviada:

```bash
termshot "nmap -sV 192.168.1.10" -v -o nmap.png
```

### Modo silencioso

O modo silencioso não mostra a saída do comando nem as mensagens do Termshot. A imagem ainda é gerada normalmente:

```bash
termshot "nmap -sV 192.168.1.10" --quiet -o nmap.png
```

Forma abreviada:

```bash
termshot "nmap -sV 192.168.1.10" -q -o nmap.png
```

`--quiet` e `--verbose` não podem ser usados juntos.

## Execução em lote

Crie um arquivo com um comando por linha:

```text
# Enumeração inicial
id
ip addr
nmap -sV 192.168.1.10
```

Execute-o com:

```bash
termshot --batch-file examples/commands.txt -o screenshots
```

Linhas vazias e linhas iniciadas com `#` são ignoradas. Uma imagem é criada para cada comando.

## Referência das opções

| Opção | Descrição |
| --- | --- |
| `command` | Comando executado pelo Bash. Deve ser informado entre aspas. |
| `-o`, `--output` | Arquivo de saída ou diretório no modo batch. |
| `--svg` | Gera SVG em vez de PNG. |
| `--title` | Define o título da janela representada. |
| `--width` | Substitui a largura detectada automaticamente. Mínimo de 20 colunas. |
| `--timeout` | Define um limite opcional em segundos. |
| `--no-timestamp` | Remove data, hora e duração da imagem. |
| `--prompt` | Adiciona uma linha de prompt antes do comando. |
| `--no-command` | Oculta a linha do comando na imagem. |
| `-q`, `--quiet` | Não mostra a saída nem as mensagens do Termshot. |
| `-v`, `--verbose` | Mostra a saída e as mensagens de controle do Termshot. |
| `--max-lines` | Limita as linhas da imagem. Use `0` para desativar. |
| `--show-tail` | Ao truncar, preserva o início e o final da saída. |
| `--batch-file` | Executa comandos listados em um arquivo. |
| `--version` | Mostra a versão instalada do Termshot. |
| `-h`, `--help` | Mostra a ajuda da linha de comando. |

## Como funciona

1. O comando é iniciado pelo Bash dentro de um pseudo-terminal.
2. O tamanho do PTY é configurado com a largura detectada ou informada.
3. A saída é reproduzida em tempo real e armazenada com os códigos ANSI.
4. Um emulador interno aplica operações de cursor e limpeza de tela.
5. Somente o estado final é enviado ao Rich.
6. O Rich gera um SVG, posteriormente convertido em PNG pelo CairoSVG.

Essa abordagem evita que cada atualização intermediária de uma barra de progresso apareça como uma nova linha na imagem final.

## Limitações

- O Termshot acompanha a saída em tempo real, mas não oferece entrada interativa completa pelo teclado.
- Programas que usam recursos de terminal não implementados podem apresentar pequenas diferenças visuais.
- Os comandos são executados com `shell=True` pelo Bash. Execute apenas comandos e arquivos em lote confiáveis.
- A compatibilidade atual é direcionada a ambientes Unix-like.

## Desenvolvimento

Clone o projeto e instale-o em modo editável:

```bash
git clone <URL_DO_REPOSITORIO>
cd termshot
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e .
```

Antes de enviar alterações, valide a sintaxe:

```bash
python3 -m py_compile termshot.py
```

## Licença

Distribuído sob a licença MIT. Consulte o arquivo [`LICENSE`](LICENSE).
