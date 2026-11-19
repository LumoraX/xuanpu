// 常驻小院（R7）：打开入口就在页面上，管家在管家台、本人可用的匠人在垫子上待命。
// 员工说一句话后，小院立刻跟上这次请求（焦点），按执行事实演出；进展卡只讲「经过」。
// 一次只演一件事：最新发出的请求，或员工在某张进展卡上点「在小院里看」的那件。
"use strict";

(function () {
  const C = () => globalThis.Courtyard;
  const stage = { scene: null, focus: null, played: new WeakMap(), roster: [], caption: null, looks: null };
  const IDLE = "管家在管家台待命，匠人们在垫子上歇着。说一句话，看他们怎么办。";

  function mounted() { return stage.scene !== null; }

  function reducedMotion() {
    try { return Boolean(globalThis.matchMedia && globalThis.matchMedia("(prefers-reduced-motion: reduce)").matches); } catch { return false; }
  }

  // 挂到页面上的 #stage；读本人可用的匠人让他们上垫子。读不到只是少几位待命的，不挡页面。
  function mount(container, options = {}) {
    if (mounted() || !container || typeof document.createElementNS !== "function") return false;
    stage.looks = options.looks || (() => C().art.loadLooks());
    const box = el("div", null, "courtyard cy-stage-wrap"), host = el("div", null, "cy-stage");
    stage.caption = el("p", IDLE, "cy-caption");
    box.append(host, stage.caption);
    container.replaceChildren(box);
    stage.scene = C().scene.create(host, {
      look: key => C().art.resolveLook(stage.looks(), key),
      onSelect: key => { if (stage.focus && options.onSelect) options.onSelect(stage.focus, key); },
    });
    stage.scene.cast([]);
    stage.scene.settle({});
    entry.cleanups.add(unmount);
    loadRoster();
    return true;
  }

  function unmount() {
    if (stage.scene) stage.scene.destroy();
    Object.assign(stage, { scene: null, focus: null, played: new WeakMap(), roster: [], caption: null });
  }

  async function loadRoster() {
    const epoch = entry.epoch;
    try {
      const body = await entryApi("GET", "/v1/assistant/artisans");
      if (isStale(epoch) || !stage.scene) return;
      stage.roster = [...new Set((body.items || []).map(i => i.package))];
      stage.scene.cast(stage.roster);
      if (!stage.focus) stage.scene.settle({});
    } catch (error) {
      if (error && error.name !== "AbortError" && error.status !== 403) console.error("小院读不到可用匠人", error);
    }
  }

  // 让小院演这件事：先把画面对齐到它此刻的事实，之后的帧再出动画。
  function focus(state) {
    if (!mounted() || !state) return;
    stage.focus = state;
    const spans = state.prev || [];
    stage.scene.cast(C().model.artisans(spans));
    stage.scene.settle(C().model.poses(spans, state.details()));
    stage.played.set(state, spans);
    stage.scene.select(state.sel);
    describe(state);
  }

  // 卡片每处理完一帧交过来；不是焦点的卡片不演（员工点「在小院里看」才切过来）。
  function update(state, frame, next, details) {
    if (!mounted()) return;
    if (state.wantsFocus) { state.wantsFocus = false; stage.focus = state; stage.played.delete(state); }
    if (stage.focus !== state) return;
    const model = C().model, before = stage.played.get(state);
    stage.scene.cast(model.artisans(next));
    if (!before || frame === "snapshot" || reducedMotion()) {
      stage.scene.settle(model.poses(next, details));
    } else {
      stage.scene.play(model.diff(before, next, details));
      // 动画是讲解，事实为准：排完后对齐到最新记录（期间又来了新帧则交给那一帧）
      stage.scene.idle().then(() => { if (stage.played.get(state) === next) stage.scene.settle(model.poses(next, state.details())); });
    }
    stage.played.set(state, next);
    describe(state);
  }

  // 小院下方一句：在演哪件事、办到哪了。
  function describe(state) {
    if (!stage.caption) return;
    const sum = C().model.summary(state.prev || [], Date.now(), state.details());
    const parts = [sum.artisans ? sum.artisans + " 位匠人" : "", sum.tools ? sum.tools + " 次工具" : "",
      sum.seconds !== null ? sum.seconds + " 秒" : ""].filter(Boolean);
    stage.caption.textContent = "这件事" + sum.state + (parts.length ? "（" + parts.join("，") + "）" : "") + "。点角色看它的经过。";
  }

  function select(state, key) { if (mounted() && stage.focus === state) stage.scene.select(key); }
  function restyle() { if (mounted()) stage.scene.restyle(); }
  function isFocus(state) { return stage.focus === state; }

  globalThis.Courtyard = globalThis.Courtyard || {};
  globalThis.Courtyard.stage = { mount, mounted, focus, update, select, restyle, isFocus };
})();
