// Embedded by build_site.py so the published page remains a single static file.
(() => {
  const config = JSON.parse(document.getElementById('ranking-config').textContent);
  const box = document.getElementById('player-box');
  const player = box.querySelector('audio');
  const title = box.querySelector('.play-title');
  const hint = box.querySelector('.play-hint');
  const official = box.querySelector('.official');
  let generation = 0, request = null, current = null, timer = null;
  function stop() {
    generation++;
    request?.abort();
    request = null;
    clearTimeout(timer);
    player.onerror = player.onplaying = null;
    player.pause();
    player.removeAttribute('src');
    player.load();
    document.querySelectorAll('button.play').forEach(b => b.setAttribute('aria-pressed', 'false'));
  }
  function playSong(button) {
    stop();
    const token = generation;
    const id = button.dataset.id;
    current = id;
    box.hidden = false;
    title.textContent = button.dataset.name || `歌曲 ${id}`;
    official.href = `https://music.163.com/song?id=${encodeURIComponent(id)}`;
    button.setAttribute('aria-pressed', 'true');
    hint.textContent = '正在加载试听…';
    player.hidden = false;
    const valid = () => token === generation;
    const fail = () => {
      if (!valid()) return;
      clearTimeout(timer);
      player.onerror = null;
      player.pause();
      player.removeAttribute('src');
      player.load();
      player.hidden = true;
      hint.textContent = '暂时无法页内试听，可能受版权、会员或网络限制。可重试或前往网易云。';
      document.querySelectorAll('button.play').forEach(b => b.setAttribute('aria-pressed', 'false'));
    };
    function audio(url, fallback) {
      if (!valid()) return;
      clearTimeout(timer);
      let finished = false;
      const onFail = () => {
        if (!valid() || finished) return;
        finished = true;
        clearTimeout(timer);
        player.onerror = null;
        fallback();
      };
      player.onerror = onFail;
      player.onplaying = () => {
        if (!valid()) return;
        clearTimeout(timer);
        hint.textContent = '正在试听；完整播放以网易云版权及账号权限为准。';
      };
      timer = setTimeout(onFail, 12000);
      player.src = url;
      const pending = player.play();
      pending?.catch(err => {
        if (!valid()) return;
        if (err.name === 'NotAllowedError') {
          clearTimeout(timer);
          hint.textContent = '浏览器暂停了自动播放，请点播放器的播放按钮。';
        } else if (err.name !== 'AbortError') onFail();
      });
    }
    async function fallback() {
      if (!valid()) return;
      if (!config.playApi) { fail(); return; }
      hint.textContent = '正在获取备用试听地址…';
      const controller = new AbortController();
      request = controller;
      const timeout = setTimeout(() => controller.abort(), 10000);
      try {
        const endpoint = new URL(config.playApi);
        if (endpoint.protocol !== 'https:') throw new Error('Unsafe endpoint');
        endpoint.searchParams.set('id', id);
        const response = await fetch(endpoint, { signal: controller.signal });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const result = await response.json();
        const url = new URL(result.url);
        if (url.protocol !== 'https:') throw new Error('Unsafe audio URL');
        if (valid()) audio(url.href, fail);
      } catch { if (valid()) fail(); }
      finally { clearTimeout(timeout); if (request === controller) request = null; }
    }
    audio(`https://music.163.com/song/media/outer/url?id=${encodeURIComponent(id)}.mp3`, fallback);
  }
  document.querySelectorAll('button.play').forEach(button => button.addEventListener('click', e => {
    e.stopPropagation();
    playSong(button);
  }));
  box.querySelector('.close-player').addEventListener('click', () => { stop(); current = null; box.hidden = true; });
  box.querySelector('.retry-player').addEventListener('click', () => {
    const button = [...document.querySelectorAll('button.play')].find(b => b.dataset.id === current);
    if (button) playSong(button);
  });
  document.querySelectorAll('.song').forEach(song => {
    const toggle = song.querySelector('.details-toggle');
    function expand() {
      const detail = song.querySelector('.detail');
      detail.hidden = !detail.hidden;
      toggle.setAttribute('aria-expanded', String(!detail.hidden));
    }
    toggle.addEventListener('click', expand);
    song.addEventListener('click', e => { if (!e.target.closest('a, button, audio')) expand(); });
  });
  let period = 30, studio = false;
  function applyFilters() {
    const query = document.getElementById('song-search').value.trim().toLocaleLowerCase();
    let matching = 0, shown = 0;
    const now = Date.now();
    document.querySelectorAll('.song').forEach(card => {
      const published = Number(card.dataset.published);
      const match = published > 0 && published <= now && now - published <= period * 86400000
        && (!studio || card.dataset.live !== '1') && card.dataset.search.includes(query);
      if (match) matching++;
      const visible = match && shown < config.top;
      card.classList.toggle('hidden', !visible);
      if (visible) {
        shown++;
        card.querySelector('.c-rank').textContent = shown;
        card.classList.toggle('top3', shown <= 3);
      } else {
        card.querySelector('.detail').hidden = true;
        card.querySelector('.details-toggle').setAttribute('aria-expanded', 'false');
      }
    });
    document.getElementById('result-count').textContent = `显示 ${shown} 首，共匹配 ${matching} 首`;
    document.getElementById('empty-state').hidden = shown > 0;
  }
  document.querySelectorAll('#filters button').forEach(button => button.addEventListener('click', () => {
    const group = button.closest('.filter-group');
    group.querySelectorAll('button').forEach(b => { b.classList.toggle('on', b === button); b.setAttribute('aria-pressed', String(b === button)); });
    if (button.dataset.time) period = button.dataset.time === 'week' ? 7 : 30;
    if (button.dataset.live) studio = button.dataset.live === 'studio';
    applyFilters();
  }));
  document.getElementById('song-search').addEventListener('input', applyFilters);
  const fresh = document.getElementById('freshness');
  if (!config.dataAt || Date.now() - config.dataAt * 1000 > 48 * 3600000) {
    fresh.hidden = false;
    fresh.textContent = config.dataAt ? '数据已超过 48 小时未更新，当前展示上次采集结果。' : '暂无有效采集时间，请稍后查看更新。';
  }
  applyFilters();
})();
