# Kwai Downloader

Baixa todos os vídeos públicos de um perfil do Kwai, a partir da URL do perfil.
Funciona por uma **interface web local** (painel no navegador) ou pela **linha de comando**.
Também pode baixar **apenas o áudio**.

> O `yt-dlp` não tem suporte ao Kwai, então este projeto não o usa. Ele abre cada vídeo num
> navegador automatizado (Playwright/Chromium), captura o `.mp4` que o player carrega e baixa o arquivo.

## Recursos

- Cole a URL do perfil e baixe tudo, ou limite a quantidade de vídeos.
- **Análise antes de baixar:** mostra quantos vídeos foram encontrados, quantos já estão na pasta
  e quantos serão baixados, e pede confirmação num modal.
- **Só áudio** (mp3, m4a, wav ou opus), usando o ffmpeg.
- **Downloads simultâneos** (1 a 8; padrão 3).
- **Progresso ao vivo:** barra geral, uma barra por vídeo no log do painel, e barras no terminal.
- **Log completo** na tela e em arquivo (`logs/kwai_dl.log`).
- **Retomada:** vídeos já baixados são pulados quando você roda de novo.
- **Seletor de pasta** nativo do sistema.

## Requisitos

- Python 3.9 ou superior (testado com 3.14)
- [ffmpeg](https://ffmpeg.org/) no PATH — **só** para o modo áudio
- Para escolher a pasta pela janela do sistema: `tkinter` (já vem no Python do Windows; no Linux,
  instale o pacote `python3-tk`)

## Instalação

```powershell
python -m pip install -r requirements.txt
```

O Chromium (usado pelo Playwright) é baixado automaticamente na primeira execução. Se preferir
baixá-lo antes:

```powershell
python -m playwright install chromium
```

> Use sempre `python -m pip` para instalar no mesmo Python que roda o painel. No PowerShell, se
> você precisar chamar o Python pelo caminho completo, use `& "C:\caminho\python.exe" -m pip ...`.

## Uso: painel web

```powershell
python app_kwai.py
```

Abre em <http://localhost:8001>.

1. Cole a **URL do perfil** (ex.: `https://www.kwai.com/@usuario`).
2. Escolha **Vídeo** ou **Só áudio** (e o formato).
3. Clique em **Escolher…** e selecione a **pasta de destino**.
4. Ajuste, se quiser, o **máx. de vídeos** e as opções em **Avançado**.
5. Clique em **Analisar e baixar**, confira a contagem no modal e confirme.

| Opção (Avançado) | O que faz |
|---|---|
| Downloads simultâneos | Quantos vídeos baixam ao mesmo tempo (1–8). |
| Pausa entre rolagens | Tempo máximo de espera por vídeos novos a cada rolagem do perfil (ms). |
| Máx. de rolagens | Limite de rolagens ao analisar o perfil. |
| Mostrar navegador | Abre o Chromium visível (útil se pedir login ou captcha). |

## Uso: linha de comando

```powershell
python kwai_dl.py "https://www.kwai.com/@usuario" -o downloads/usuario
python kwai_dl.py "https://www.kwai.com/@usuario" --audio --formato-audio m4a --paralelo 4
```

| Argumento | Padrão | Descrição |
|---|---|---|
| `url` | — | URL do perfil. |
| `-o`, `--saida` | `downloads` | Pasta de destino. |
| `--audio` | desligado | Baixa só o áudio (requer ffmpeg). |
| `--formato-audio` | `mp3` | Formato do áudio. |
| `--paralelo` | `3` | Downloads simultâneos (1–8). |
| `--limite` | `0` | Máx. de vídeos (0 = todos). |
| `--max-scrolls` | `200` | Máx. de rolagens ao analisar. |
| `--pausa-ms` | `1500` | Espera máxima por vídeos novos a cada rolagem. |
| `--headful` | desligado | Mostra o navegador. |

Na linha de comando não há modal de confirmação: o download começa logo após a análise.

## Estrutura

```
app_kwai.py        servidor local do painel (porta 8001)
painel_kwai.html   interface web
kwai_dl.py         lógica de análise e download (módulo e CLI)
requirements.txt   dependências
docs/              documentação técnica
logs/              log gerado em execução (ignorado pelo git)
```

## Solução de problemas

| Sintoma | Causa e solução |
|---|---|
| `No module named 'playwright'` | O Playwright foi instalado em outro Python. Rode `python -m pip install -r requirements.txt` com o mesmo Python que roda o painel. |
| `Executable doesn't exist ... chromium` | O navegador ainda não foi baixado. O programa tenta baixar sozinho; se falhar, rode `python -m playwright install chromium`. |
| `module 'kwai_dl' has no attribute 'executar'` | O `kwai_dl.py` na pasta é uma versão antiga. Substitua pela versão atual. |
| `Unexpected token '-m'` no PowerShell | Caminho entre aspas precisa do `&` na frente: `& "C:\...\python.exe" -m pip ...`. |
| "Nenhum vídeo encontrado" | Ative **Mostrar navegador** para ver se o Kwai pede login ou captcha. Aumente a pausa entre rolagens. |
| Vários vídeos com `FALHOU` | Diminua os downloads simultâneos para 2 ou 3. Rode de novo: os já baixados são pulados. |
| Não abre a janela de escolher pasta | `tkinter` indisponível. No Linux, instale `python3-tk`. |
| Modo áudio recusa começar | Instale o ffmpeg e deixe-o no PATH. |

## Aviso

Baixe apenas conteúdo que você tem direito de usar e respeite os termos de uso do Kwai e os
direitos autorais dos criadores. O site pode mudar a qualquer momento e quebrar a captura; veja
a seção "Limitações" na documentação técnica.

Mais detalhes em [docs/DOCUMENTACAO.md](docs/DOCUMENTACAO.md).
