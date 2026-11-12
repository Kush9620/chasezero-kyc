let audioCtx: AudioContext | null = null;

function getAudioContext(): AudioContext | null {
  if (typeof window === "undefined") return null;
  const Ctx =
    window.AudioContext ||
    (window as unknown as { webkitAudioContext?: typeof AudioContext })
      .webkitAudioContext;
  if (!Ctx) return null;
  if (!audioCtx) audioCtx = new Ctx();
  if (audioCtx.state === "suspended") {
    audioCtx.resume().catch(() => {});
  }
  return audioCtx;
}

function playTone(
  freq: number,
  durationMs: number,
  type: OscillatorType = "sine",
  gainValue = 0.04,
  delayMs = 0
) {
  try {
    const ctx = getAudioContext();
    if (!ctx) return;
    const start = ctx.currentTime + delayMs / 1000;
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = type;
    osc.frequency.setValueAtTime(freq, start);
    gain.gain.setValueAtTime(0.0001, start);
    gain.gain.exponentialRampToValueAtTime(gainValue, start + 0.015);
    gain.gain.exponentialRampToValueAtTime(0.0001, start + durationMs / 1000);
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.start(start);
    osc.stop(start + durationMs / 1000 + 0.02);
  } catch {
    // Ignore audio context errors when blocked by browser autoplay policy
  }
}

export const soundFx = {
  playNodePing(enabled: boolean) {
    if (!enabled) return;
    playTone(660, 110, "sine", 0.035);
    playTone(880, 140, "sine", 0.03, 70);
  },
  playDraftTick(enabled: boolean) {
    if (!enabled) return;
    playTone(520, 35, "triangle", 0.015);
  },
  playCriticReject(enabled: boolean) {
    if (!enabled) return;
    playTone(330, 140, "triangle", 0.045, 0);
    playTone(240, 220, "triangle", 0.045, 110);
  },
  playCriticPass(enabled: boolean) {
    if (!enabled) return;
    playTone(587.33, 120, "sine", 0.04, 0);
    playTone(783.99, 140, "sine", 0.04, 90);
    playTone(1046.5, 220, "sine", 0.035, 180);
  },
  playDispatchSuccess(enabled: boolean) {
    if (!enabled) return;
    playTone(523.25, 120, "sine", 0.045, 0);
    playTone(659.25, 120, "sine", 0.045, 80);
    playTone(783.99, 140, "sine", 0.045, 160);
    playTone(1046.5, 300, "sine", 0.05, 240);
  },
};
