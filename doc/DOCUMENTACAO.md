# Documentação técnica — Kwai Downloader

## 1. Visão geral

Ferramenta local que baixa os vídeos públicos de um perfil do Kwai. Como o `yt-dlp` não suporta
o Kwai, o download é feito controlando um navegador (Playwright + Chromium): o script abre o perfil,
coleta os links dos vídeos, abre cada vídeo, intercepta a requisição do `.mp4` e baixa o arquivo.

Componentes:

| Arquivo | Papel |
|---|---|
| `kwai_dl.py` | Núcleo: análise do perfil, captura, download, extração de áudio, barras no terminal. Também é a CLI. |
| `app_kwai.py` | Servidor HTTP local (biblioteca padrão) que expõe o núcleo ao painel e guarda o estado da execução. |
| `painel_kwai.html` | Interface web (HTML/CSS/JS puro, sem dependências). Consulta a API a cada 0,5–1 s. |
| `requirements.txt` | Dependência única: `playwright`. |

Dependências externas: `playwright` (Python), Chromium (baixado pelo Playwright), `ffmpeg` (apenas modo
áudio), `tkinter` (apenas o seletor de pasta).

## 2. Arquitetura

```
 navegador (painel_kwai.html)
        │  HTTP/JSON (localhost:8001)
        ▼
 app_kwai.py ── ThreadingHTTPServer (1 thread por requisição)
        │  cria 1 thread de trabalho por execução
        ▼
 kwai_dl.executar()  ──►  asyncio.run(_executar)  ──►  Playwright (Chromium)
        │                                               ├─ 1 página: analisa o perfil
        │ callbacks: log / evento / confirmar            └─ N páginas: capturam os .mp4
        ▼
 estado compartilhado (dict + Lock) ◄── lido por GET /api/estado
```

### Threads e concorrência

- O servidor atende cada requisição em uma thread. O estado da execução (`estado`) é um dicionário
  protegido por um `threading.Lock`.
- Cada execução roda numa **thread de trabalho** própria, que chama `kwai_dl.executar()`. Esta função
  é síncrona e chama `asyncio.run()` por dentro; o Playwright roda na API assíncrona dentro desse loop.
- O download dos arquivos (urllib) e o ffmpeg rodam em threads auxiliares via `asyncio.to_thread`.
- Só uma execução por vez: iniciar com outra em andamento retorna erro.
- Parada e cancelamento usam `threading.Event` (`parar_evt`) verificado entre etapas e a cada bloco baixado.

## 3. Fluxo de uma execução

1. **Início** (`POST /api/iniciar`): valida URL e pasta; estado vai para `analisando`.
2. **Análise** (`coletar_links`):
   - abre o perfil (`domcontentloaded`) e espera 4 s;
   - lê todos os `a[href]` e guarda os que casam com `PADRAO_LINK` (`/(video|photo|short-video)/`);
   - rola a página (`mouse.wheel(0, 4000)`) e, a cada rolagem, espera até `pausa_ms` em passos de 250 ms,
     saindo assim que aparece link novo;
   - termina com 4 rolagens seguidas sem novidade, ao atingir `max_scrolls` ou `limite`.
3. **Resumo e confirmação:** calcula `total`, `existentes` (já na pasta) e `novos`. O estado vai para
   `aguardando` e `resumo` é preenchido; o painel abre o modal. A thread de trabalho espera (até 30 min)
   a resposta de `POST /api/confirmar`. Cancelar aciona `parar_evt`.
4. **Download** (estado `baixando`): uma tarefa assíncrona por vídeo, limitada por `asyncio.Semaphore(paralelo)`:
   1. se o arquivo já existe → `pulado`;
   2. `sniffar_mp4`: abre a página do vídeo e espera até 12 s pela URL do `.mp4`;
   3. `baixar_direto`: baixa em blocos de 256 KiB, informando o progresso;
   4. no modo áudio, `extrair_audio` converte com ffmpeg e apaga o `.mp4`;
   5. emite o resultado (`ok` ou `falhou`).
5. **Fim:** fecha o navegador; estado `concluido` (ou `parado` / `erro`).

## 4. Detalhes do núcleo (`kwai_dl.py`)

### 4.1 Captura do `.mp4` (`sniffar_mp4`)
- Abre uma página nova no contexto e escuta `response`. Considera `.mp4` toda resposta com
  `content-type: video/mp4` ou URL terminada em `.mp4` (com ou sem query).
- A cada 0,4 s também lê `currentSrc`/`src` das tags `<video>` (ignora `blob:`).
- Devolve a primeira URL encontrada, ou `None` após 12 s.
- Imagens e fontes são bloqueadas no contexto (`context.route`) para acelerar. Mídia **não** é bloqueada.
- Contexto emulando Chrome mobile (Android), viewport 430×900.

### 4.2 Download (`baixar_direto` / `_stream`)
- Usa `urllib` com `User-Agent`, `Referer` (página do vídeo) e `Cookie` copiado do contexto do navegador.
- Grava em `<id>.mp4.part` e renomeia para `<id>.mp4` ao terminar (`os.replace`). Em erro ou
  interrupção, o `.part` é apagado.
- Se o download direto falhar, usa o plano B: `context.request.get()` (pelo próprio navegador), sem
  progresso detalhado.

### 4.3 Nomes e retomada
- O **id** do vídeo é o último segmento da URL (sem query). Arquivos: `<id>.mp4` ou `<id>.<formato>`.
- `ja_baixado`: no modo áudio, procura `<id>.<formato>`; no modo vídeo, procura `<id>.*` sem `.part`.
  Rodar de novo continua de onde parou.

### 4.4 Áudio
`ffmpeg -y -loglevel error -i <mp4> -vn <id>.<formato>`; em caso de sucesso o `.mp4` é removido.
O modo áudio verifica o ffmpeg no PATH antes de começar (`RuntimeError` se ausente).

### 4.5 Chromium automático (`abrir_navegador`)
Se o `launch` falhar com "Executable doesn't exist", roda `python -m playwright install chromium`
(mesmo interpretador) e tenta de novo uma vez.

### 4.6 Barras no terminal (`Console`)
- Singleton `console`. Mostra uma linha por vídeo em andamento e uma linha `TOTAL`, redesenhadas a cada
  0,15 s por uma thread própria usando sequências ANSI (cursor para cima + limpar abaixo).
- Mensagens de log passam por `console.print`, que apaga as barras, imprime a linha e redesenha.
- Fases sem tamanho conhecido (abrindo página, extraindo áudio) usam barra animada.
- Só ativa se `stdout` for um terminal. No Windows liga o modo ANSI via `SetConsoleMode`.
  Se o terminal não suportar `█░`, usa `#-`.

### 4.7 API do módulo

```python
executar(url, saida="downloads", audio=False, formato="mp3", max_scrolls=200,
         pausa_ms=1500, headful=False, limite=0, log=console.print, evento=None,
         parar=None, confirmar=None, paralelo=3) -> (ok, falhas, puladas)
```

| Parâmetro | Descrição |
|---|---|
| `log(msg)` | Chamado com cada linha de log. |
| `evento(tipo, **dados)` | Notificações estruturadas (tabela abaixo). |
| `parar` | `threading.Event`; quando acionado, a execução para. |
| `confirmar(info) -> bool` | Chamado após a análise; `False` cancela. `None` = sem confirmação. |
| `paralelo` | Limitado ao intervalo 1–8. |

`info` (resumo): `total`, `existentes`, `novos`, `audio`, `formato`, `pasta`.

Eventos:

| Tipo | Dados | Quando |
|---|---|---|
| `encontrados` | `n` | A cada rolagem da análise. |
| `total` | `total` | Após a confirmação, antes de baixar. |
| `item` | `id`, `status`, `metodo?` | `status`: `pendente`, `baixando`, `ok`, `falhou`, `pulado`. |
| `progresso` | `id`, `fase`, `atual`, `total`, `vel` | `fase`: `abrindo`, `baixando`, `audio`. `vel` em bytes/s. |

## 5. API HTTP (`app_kwai.py`)

Servidor em `127.0.0.1:8001` (`ThreadingHTTPServer`). Todas as respostas são JSON, exceto `GET /`.
Erros de validação voltam como `{"erro": "mensagem"}`.

| Método e rota | Corpo | Resposta |
|---|---|---|
| `GET /` | — | `painel_kwai.html` |
| `GET /api/estado?desde=N` | — | Estado (abaixo). `logs` traz só as linhas a partir do índice `N`. |
| `POST /api/iniciar` | `url`, `pasta`, `modo` (`video`/`audio`), `formato` (`mp3`/`m4a`/`wav`/`opus`), `limite`, `paralelo`, `pausa_ms`, `max_scrolls`, `headful` | `{"ok": true}` |
| `POST /api/confirmar` | `sim` (bool) | `{"ok": true}` |
| `POST /api/parar` | — | `{"ok": true}` |
| `POST /api/escolher-pasta` | `inicial?` | `{"pasta": "..."}`, `{"cancelado": true}` ou `{"erro": ...}` |
| `POST /api/abrir-pasta` | — | `{"ok": true}` |

### Estado (`GET /api/estado`)

```json
{
  "status": "baixando",
  "total": 40, "ok": 3, "falhas": 0, "puladas": 1,
  "encontrados": 40, "pasta": "C:\\...\\downloads",
  "resumo": null,
  "logs": ["12:01:02  ..."], "n_logs": 87,
  "itens": [{"id": "123", "status": "baixando", "fase": "baixando",
             "atual": 1048576, "total": 3145728, "vel": 524288}]
}
```

`status`: `ocioso`, `analisando`, `aguardando`, `baixando`, `concluido`, `parado`, `erro`.
`resumo` só vem preenchido em `aguardando`.

### Segurança
- O servidor escuta só em `127.0.0.1`.
- Toda rota confere o cabeçalho `Host` (`localhost` ou `127.0.0.1`), o que bloqueia DNS rebinding.
- `POST` exige `Content-Type: application/json`, o que impede que outros sites disparem ações
  por formulário.
- A pasta de destino precisa existir (`is_dir`).
- O painel não tem autenticação: não exponha a porta fora da máquina.

### Seletor de pasta
`escolher_pasta` roda um subprocesso Python com `tkinter.filedialog.askdirectory` (janela
`topmost`) e devolve o caminho. Fica em subprocesso porque o tkinter exige a thread principal em
alguns sistemas.

## 6. Interface (`painel_kwai.html`)

- Faz `fetch` em `/api/estado` a cada 500 ms durante a execução e 1 s em repouso.
- Barra geral e contadores: derivados de `ok + puladas + falhas` sobre `total`.
- Barras por vídeo: renderizadas no card do log para os itens com `status = baixando`, atualizando
  nós DOM por `id`; somem quando o item termina.
- Modal de confirmação: `<dialog>` aberto quando `status = aguardando`; fechar ou cancelar envia
  `POST /api/confirmar {sim:false}`.
- A pasta escolhida é guardada em `localStorage`.

## 7. Logs

- Tela: card de log do painel (cores: verde = sucesso, vermelho = erro/cancelamento, laranja = análise/espera).
- Terminal: mesmas linhas, acima das barras.
- Arquivo: `logs/kwai_dl.log` (ao lado de `app_kwai.py`), com uma linha por evento, acrescentada ao final, já com data e hora. O arquivo não é limpo
  automaticamente.

## 8. Limitações conhecidas

- **Depende do site.** A análise usa o padrão de link `/video/` e a captura espera uma resposta `video/mp4`.
  Mudanças no Kwai (estrutura de links, streaming em HLS/DASH, proteções anti-bot) podem exigir ajustes em
  `PADRAO_LINK` e `sniffar_mp4`. Este projeto foi desenvolvido sem teste contra o Kwai real.
- **Contagem é estimativa.** O total é o que o Kwai carregou ao rolar o perfil; pode haver mais vídeos.
- **Possível captura errada.** Se a página de um vídeo pré-carregar o vídeo seguinte, a primeira URL `.mp4`
  vista pode não ser a do vídeo aberto.
- **Conteúdo público apenas.** Não há login; perfis ou vídeos que exigem autenticação não são suportados
  (use "Mostrar navegador" para resolver captcha/login pontualmente).
- **Streaming segmentado não é suportado.** Vídeos servidos em segmentos (`.m3u8`/`.mpd`) não são baixados.
- **Limite de requisições.** Muitos downloads simultâneos podem causar bloqueio temporário pelo site.
- Uma execução por vez; o estado vive em memória e se perde ao fechar o servidor (o log em arquivo permanece).

## 9. Como estender

| Objetivo | Onde mexer |
|---|---|
| Reconhecer outro formato de link de vídeo | `PADRAO_LINK` em `kwai_dl.py` |
| Mudar como o `.mp4` é encontrado | `sniffar_mp4` (condição em `on_response`, tempo de espera) |
| Alterar nome dos arquivos | `id_do_link` e os pontos que montam `destino` em `_executar` |
| Novo formato de áudio | `extrair_audio` e a lista permitida em `iniciar` (`app_kwai.py`) |
| Limite de simultâneos | `max(1, min(8, ...))` em `executar` e em `iniciar` |
| Porta do painel | constante `PORTA` em `app_kwai.py` |

## 10. Dicas de depuração

- Rode com **Mostrar navegador** (`--headful`) para ver o que o Kwai exibe.
- Reduza para `--paralelo 1` para ler o log de um vídeo por vez.
- Teste o núcleo sem o painel: `python kwai_dl.py "<url>" --limite 3 -o teste`.
- Se um vídeo falha com "não achei o arquivo de vídeo no player", copie as linhas do log desse vídeo: elas mostram o
  link aberto e quanto tempo esperou.
