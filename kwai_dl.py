#!/usr/bin/env python3
"""
kwai_dl.py - baixa todos os vídeos públicos de um perfil do Kwai (CLI e módulo).

CLI:
    python kwai_dl.py "https://www.kwai.com/@usuario" -o downloads/usuario
    python kwai_dl.py "https://www.kwai.com/@usuario" --audio --formato-audio m4a --paralelo 4

Interface web: python app_kwai.py  (abre em http://localhost:8001)

Instalação: pip install -r requirements.txt  (o Chromium é baixado sozinho na 1ª vez)
O modo áudio também precisa do ffmpeg no PATH.

Fluxo: rola o perfil coletando links -> (opcional) pede confirmação com a contagem
-> abre vários vídeos ao mesmo tempo no navegador, captura o .mp4 que o player carrega
e baixa o arquivo (o yt-dlp não suporta o Kwai, então não é usado).
Vídeos já baixados são pulados (dá pra rodar de novo e continuar).
"""
import argparse
import asyncio
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

PADRAO_LINK = re.compile(r"/(video|photo|short-video)/", re.I)
UA = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36"
)


class Console:
    """Barras de progresso no terminal: uma por vídeo em andamento + uma de total.
    Mensagens de log saem acima das barras. Sem terminal (saída redirecionada), só imprime."""

    def __init__(self):
        self.lock = threading.RLock()
        self.barras = {}
        self.total = 0
        self.feitos = 0
        self.n_linhas = 0
        self.frame = 0
        self.ativo = False
        self._rodando = False
        self.cheia, self.vazia = "█", "░"
        try:
            self.ativo = sys.stdout.isatty()
            "█░".encode(sys.stdout.encoding or "ascii")
        except Exception:
            self.cheia, self.vazia = "#", "-"
        if self.ativo and os.name == "nt":  # liga o modo ANSI do console do Windows
            try:
                import ctypes
                k = ctypes.windll.kernel32
                h = k.GetStdHandle(-11)
                m = ctypes.c_uint32()
                k.GetConsoleMode(h, ctypes.byref(m))
                k.SetConsoleMode(h, m.value | 4)
            except Exception:
                os.system("")

    # --- desenho ---
    def _apagar(self):
        if self.n_linhas:
            sys.stdout.write(f"\x1b[{self.n_linhas}A\r\x1b[J")
            self.n_linhas = 0

    def _barra(self, frac, larg=22):
        n = int(larg * frac)
        return self.cheia * n + self.vazia * (larg - n)

    def _indet(self, larg=22):
        pos = self.frame % (larg - 4)
        return self.vazia * pos + self.cheia * 4 + self.vazia * (larg - 4 - pos)

    def _linha_video(self, nome, b):
        mb = lambda x: f"{x / 1_048_576:.1f}"
        if b["fase"] == "baixando" and b["total"]:
            frac = min(1.0, b["atual"] / b["total"])
            vel = b["atual"] / max(0.1, time.time() - b["t0"]) / 1_048_576
            return (f"{nome:<14} {self._barra(frac)} {frac * 100:5.1f}%  "
                    f"{mb(b['atual'])}/{mb(b['total'])} MB  {vel:.1f} MB/s")
        txt = {"abrindo": "abrindo página...", "audio": "extraindo áudio...",
               "baixando": f"baixando... {mb(b['atual'])} MB"}.get(b["fase"], b["fase"])
        return f"{nome:<14} {self._indet()}  {txt}"

    def _desenhar(self):
        cols = shutil.get_terminal_size((100, 20)).columns - 1
        linhas = [self._linha_video(str(k)[:14], b) for k, b in self.barras.items()]
        if self.total:
            frac = self.feitos / self.total
            linhas.append(f"{'TOTAL':<14} {self._barra(frac)} {frac * 100:5.1f}%  "
                          f"{self.feitos}/{self.total} vídeos")
        for l in linhas:
            sys.stdout.write(l[:cols] + "\n")
        self.n_linhas = len(linhas)

    def _redesenhar(self):
        if not self.ativo:
            return
        with self.lock:
            self._apagar()
            self._desenhar()
            sys.stdout.flush()

    # --- API ---
    def print(self, msg):
        with self.lock:
            if self.ativo:
                self._apagar()
            sys.stdout.write(str(msg) + "\n")
            if self.ativo:
                self._desenhar()
            sys.stdout.flush()

    def iniciar(self, total):
        with self.lock:
            self.total, self.feitos, self.barras = total, 0, {}
            self._rodando = True
        if self.ativo:
            threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while self._rodando:
            self.frame += 1
            self._redesenhar()
            time.sleep(0.15)

    def adicionar(self, vid):
        with self.lock:
            self.barras[vid] = dict(fase="abrindo", atual=0, total=0, t0=time.time())

    def atualizar(self, vid, fase=None, atual=None, total=None):
        b = self.barras.get(vid)
        if not b:
            return
        if fase and fase != b["fase"]:
            b["fase"] = fase
            if fase == "baixando":
                b["t0"] = time.time()
        if atual is not None:
            b["atual"] = atual
        if total is not None:
            b["total"] = total

    def remover(self, vid):
        with self.lock:
            self.barras.pop(vid, None)

    def concluir_um(self):
        with self.lock:
            self.feitos += 1

    def encerrar(self):
        self._rodando = False
        with self.lock:
            self.barras.clear()
            if self.ativo:
                self._apagar()
                sys.stdout.flush()
            self.total = 0


console = Console()


def id_do_link(link):
    return link.split("?")[0].rstrip("/").split("/")[-1]


def ja_baixado(saida, vid, audio, formato):
    if audio:
        return (saida / f"{vid}.{formato}").exists()
    return any(saida.glob(f"{vid}.*")) and not any(saida.glob(f"{vid}.*.part"))


def parou(parar):
    return parar is not None and parar.is_set()


def tamanho(saida, vid):
    arqs = [f for f in saida.glob(f"{vid}.*") if f.suffix != ".part"]
    return f"{arqs[0].stat().st_size / 1_048_576:.1f} MB" if arqs else "?"


def extrair_audio(video, formato):
    destino = video.with_suffix(f".{formato}")
    r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(video),
                        "-vn", str(destino)], capture_output=True)
    if r.returncode == 0 and destino.exists() and destino.stat().st_size > 0:
        video.unlink(missing_ok=True)
        return True
    destino.unlink(missing_ok=True)
    return False


async def abrir_navegador(p, headful, log):
    """Abre o Chromium; se ainda não foi baixado, instala sozinho e tenta de novo."""
    try:
        return await p.chromium.launch(headless=not headful)
    except Exception as e:
        if "Executable doesn't exist" not in str(e):
            raise
    log("Chromium ainda não instalado. Baixando automaticamente (pode levar alguns minutos)...")
    r = await asyncio.to_thread(
        subprocess.run, [sys.executable, "-m", "playwright", "install", "chromium"],
        capture_output=True, text=True, errors="replace")
    if r.returncode != 0:
        raise RuntimeError("Falha ao instalar o Chromium: "
                           + (r.stderr or r.stdout or "sem detalhes").strip()[-300:])
    log("Chromium instalado")
    return await p.chromium.launch(headless=not headful)


async def coletar_links(page, url, max_scrolls, pausa_ms, limite, log, parar, evento):
    log(f"Abrindo perfil: {url}")
    await page.goto(url, wait_until="domcontentloaded")
    await page.wait_for_timeout(4000)
    links, vistos = [], set()

    async def varrer():
        hrefs = await page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
        novos = 0
        for h in hrefs:
            h = h.split("#")[0]
            if PADRAO_LINK.search(h) and h not in vistos:
                vistos.add(h)
                links.append(h)
                novos += 1
        return novos

    n0 = await varrer()
    log(f"Primeira leitura: {len(links)} vídeos encontrados")
    evento("encontrados", n=len(links))
    parados = 0 if n0 else 1
    for n in range(1, max_scrolls + 1):
        if parou(parar):
            break
        if limite and len(links) >= limite:
            log(f"Limite de {limite} vídeos atingido")
            break
        await page.mouse.wheel(0, 4000)
        novos, esperado = 0, 0
        while esperado < pausa_ms:  # sai assim que aparecer vídeo novo
            await page.wait_for_timeout(250)
            esperado += 250
            novos = await varrer()
            if novos:
                break
        log(f"Rolagem {n}/{max_scrolls}: {len(links)} vídeos encontrados (+{novos})")
        evento("encontrados", n=len(links))
        parados = 0 if novos else parados + 1
        if parados >= 4:
            log("Fim do perfil (4 rolagens sem novidades)")
            break
    return links[:limite] if limite else links


async def sniffar_mp4(context, link, espera_s=12):
    """Abre o vídeo e devolve a URL do .mp4 assim que o player a pedir."""
    page = await context.new_page()
    achados = []

    def on_response(r):
        try:
            ct = r.headers.get("content-type", "")
        except Exception:
            ct = ""
        if "video/mp4" in ct or re.search(r"\.mp4(\?|$)", r.url):
            achados.append(r.url)

    page.on("response", on_response)
    try:
        try:
            await page.goto(link, wait_until="commit")
        except Exception:
            return None
        for _ in range(int(espera_s / 0.4)):
            if achados:
                return achados[0]
            try:
                srcs = await page.eval_on_selector_all(
                    "video", "vs => vs.map(v => v.currentSrc || v.src)")
                srcs = [s for s in srcs if s and s.startswith("http")]
                if srcs:
                    return srcs[0]
            except Exception:
                pass  # página ainda navegando
            await asyncio.sleep(0.4)
        return achados[0] if achados else None
    finally:
        await page.close()


def _stream(url, destino, headers, cb, parar):
    """Baixa em blocos para poder mostrar o progresso. Grava em .part e renomeia no fim."""
    parcial = destino.with_name(destino.name + ".part")
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as r, open(parcial, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            baixado = 0
            cb(0, total)
            while True:
                bloco = r.read(256 * 1024)
                if not bloco:
                    break
                f.write(bloco)
                baixado += len(bloco)
                cb(baixado, total)
                if parou(parar):
                    raise InterruptedError
        os.replace(parcial, destino)
    except BaseException:
        parcial.unlink(missing_ok=True)
        raise


async def baixar_direto(context, mp4_url, link, destino, cb, parar):
    cookies = await context.cookies(mp4_url)
    headers = {"User-Agent": UA, "Referer": link, "Accept": "*/*"}
    if cookies:
        headers["Cookie"] = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
    try:
        await asyncio.to_thread(_stream, mp4_url, destino, headers, cb, parar)
        return destino.stat().st_size > 0
    except InterruptedError:
        raise
    except Exception:  # plano B: baixa pelo próprio navegador (sem progresso detalhado)
        resp = await context.request.get(mp4_url, headers={"Referer": link})
        if not resp.ok:
            return False
        corpo = await resp.body()
        await asyncio.to_thread(destino.write_bytes, corpo)
        return destino.stat().st_size > 0


async def _executar(url, saida, audio, formato, max_scrolls, pausa_ms, headful, limite,
                    log, evento, parar, confirmar, paralelo):
    from playwright.async_api import async_playwright

    ok = falhas = puladas = 0
    async with async_playwright() as p:
        log("Iniciando navegador...")
        browser = await abrir_navegador(p, headful, log)
        try:
            context = await browser.new_context(user_agent=UA,
                                                viewport={"width": 430, "height": 900})
            context.set_default_timeout(20000)

            async def filtro(route):  # imagens e fontes não servem pra nada aqui
                if route.request.resource_type in ("image", "font"):
                    await route.abort()
                else:
                    await route.continue_()

            await context.route("**/*", filtro)

            page = await context.new_page()
            links = await coletar_links(page, url, max_scrolls, pausa_ms, limite, log,
                                        parar, evento)
            await page.close()
            if parou(parar):
                log("Interrompido durante a análise")
                return 0, 0, 0
            if not links:
                log("Nenhum vídeo encontrado. Tente 'mostrar navegador' (login/captcha?).")
                return 0, 0, 0
            existentes = sum(ja_baixado(saida, id_do_link(l), audio, formato) for l in links)
            info = dict(total=len(links), existentes=existentes, novos=len(links) - existentes,
                        audio=audio, formato=formato, pasta=str(saida.resolve()))
            log(f"Análise concluída: {info['total']} vídeos no perfil, "
                f"{existentes} já estão na pasta, {info['novos']} para baixar")
            if confirmar:
                log("Aguardando confirmação do usuário...")
                if not await asyncio.get_running_loop().run_in_executor(None, confirmar, info):
                    log("Download cancelado pelo usuário")
                    return 0, 0, 0
                log("Confirmado. Iniciando downloads")
            evento("total", total=len(links))
            for l in links:
                evento("item", id=id_do_link(l), status="pendente")
            log(f"Baixando {len(links)} vídeos, {paralelo} ao mesmo tempo")

            sem = asyncio.Semaphore(paralelo)
            c = {"ok": 0, "falhas": 0, "puladas": 0}
            console.iniciar(len(links))

            async def tarefa(i, link):
                vid = id_do_link(link)
                pre = f"[{i}/{len(links)}] {vid}"
                if ja_baixado(saida, vid, audio, formato):
                    log(f"{pre}: já existe, pulando")
                    evento("item", id=vid, status="pulado")
                    c["puladas"] += 1
                    console.concluir_um()
                    return
                async with sem:
                    if parou(parar):
                        return
                    evento("item", id=vid, status="baixando")
                    log(f"{pre}: abrindo {link} e capturando o .mp4 do player...")
                    console.adicionar(vid)
                    evento("progresso", id=vid, fase="abrindo", atual=0, total=0, vel=0)
                    inicio = time.time()
                    destino = saida / f"{vid}.mp4"
                    try:
                        mp4 = await sniffar_mp4(context, link)
                        marca = {"t": 0.0, "t0": time.time()}
                        if mp4:
                            console.atualizar(vid, "baixando")
                            evento("progresso", id=vid, fase="baixando", atual=0, total=0, vel=0)

                        def cb(a, t):
                            console.atualizar(vid, "baixando", a, t)
                            agora = time.time()
                            if agora - marca["t"] >= 0.25 or (t and a >= t):
                                marca["t"] = agora
                                evento("progresso", id=vid, fase="baixando", atual=a, total=t,
                                       vel=a / max(0.1, agora - marca["t0"]))
                        if not (mp4 and await baixar_direto(context, mp4, link, destino,
                                                            cb, parar)):
                            raise RuntimeError("não achei o arquivo de vídeo no player")
                        if audio:
                            console.atualizar(vid, "audio")
                            evento("progresso", id=vid, fase="audio", atual=0, total=0, vel=0)
                            log(f"{pre}: extraindo áudio com ffmpeg...")
                            if not await asyncio.to_thread(extrair_audio, destino, formato):
                                raise RuntimeError("falha ao extrair o áudio")
                    except InterruptedError:
                        log(f"{pre}: interrompido")
                        evento("item", id=vid, status="pendente")
                        return
                    except Exception as e:
                        log(f"{pre}: FALHOU ({e})")
                        evento("item", id=vid, status="falhou")
                        c["falhas"] += 1
                        console.concluir_um()
                        return
                    finally:
                        console.remover(vid)
                    log(f"{pre}: ok ({tamanho(saida, vid)}, {time.time() - inicio:.1f}s)")
                    evento("item", id=vid, status="ok", metodo="navegador")
                    c["ok"] += 1
                    console.concluir_um()

            await asyncio.gather(*(tarefa(i, l) for i, l in enumerate(links, 1)))
            if parou(parar):
                log("Interrompido pelo usuário")
            ok, falhas, puladas = c["ok"], c["falhas"], c["puladas"]
        finally:
            console.encerrar()
            await browser.close()
            log("Navegador fechado")
    log(f"Concluído: {ok} baixados, {puladas} pulados, {falhas} falhas")
    return ok, falhas, puladas


def executar(url, saida="downloads", audio=False, formato="mp3", max_scrolls=200,
             pausa_ms=1500, headful=False, limite=0, log=console.print, evento=None, parar=None,
             confirmar=None, paralelo=3):
    """Baixa o perfil. evento(tipo, **dados): 'encontrados', 'total' e 'item'
    (id, status, metodo). confirmar(info) -> bool é chamado depois da análise, antes
    de baixar. Retorna (ok, falhas, puladas)."""
    evento = evento or (lambda *a, **k: None)
    if audio and not shutil.which("ffmpeg"):
        raise RuntimeError("O modo áudio precisa do ffmpeg instalado e no PATH.")
    saida = Path(saida)
    saida.mkdir(parents=True, exist_ok=True)
    paralelo = max(1, min(8, int(paralelo)))
    log(f"Pasta de saída: {saida.resolve()} | modo: "
        f"{'só áudio (' + formato + ')' if audio else 'vídeo'} | simultâneos: {paralelo}")
    return asyncio.run(_executar(url, saida, audio, formato, max_scrolls, pausa_ms, headful,
                                 limite, log, evento, parar, confirmar, paralelo))


def main():
    ap = argparse.ArgumentParser(description="Baixa todos os vídeos de um perfil do Kwai")
    ap.add_argument("url")
    ap.add_argument("-o", "--saida", default="downloads")
    ap.add_argument("--max-scrolls", type=int, default=200)
    ap.add_argument("--pausa-ms", type=int, default=1500)
    ap.add_argument("--limite", type=int, default=0, help="máx. de vídeos (0 = todos)")
    ap.add_argument("--paralelo", type=int, default=3, help="downloads simultâneos (1-8)")
    ap.add_argument("--audio", action="store_true", help="baixa apenas o áudio (requer ffmpeg)")
    ap.add_argument("--formato-audio", default="mp3")
    ap.add_argument("--headful", action="store_true", help="mostra o navegador")
    a = ap.parse_args()
    executar(a.url, a.saida, a.audio, a.formato_audio, a.max_scrolls, a.pausa_ms,
             a.headful, a.limite, paralelo=a.paralelo)


if __name__ == "__main__":
    main()
