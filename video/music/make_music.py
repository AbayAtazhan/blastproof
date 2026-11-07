"""Original 90-second soundtrack for the Blastproof demo, synthesized from scratch (no samples).

120 BPM, A minor, cut to the video's scenes:
  0-14.5   intro: ticking hats, dark pad, glitch on "secrets" (5.0 s), riser into the reveal
  14.5     impact + drop: full beat, bass, arpeggio
  53-61    breakdown + riser ("Now we ask it to fetch data from the internet...")
  61.0     heavy impact on "BLOCKED."
  61-84.5  beat returns
  84.5     final hit on the logo, then a fade to silence at 90 s

python make_music.py out.wav
"""

import sys

import numpy as np
from scipy.io import wavfile
from scipy.signal import butter, fftconvolve, sosfilt

SR = 44100
DUR = 90.0
N = int(SR * DUR)
BPM = 120
BEAT = 60 / BPM
rng = np.random.default_rng(7)
t_all = np.arange(N) / SR


def lp(x, hz, order=2):
    return sosfilt(butter(order, hz, "low", fs=SR, output="sos"), x)


def hp(x, hz, order=2):
    return sosfilt(butter(order, hz, "high", fs=SR, output="sos"), x)


def bp(x, lo, hi, order=2):
    return sosfilt(butter(order, [lo, hi], "band", fs=SR, output="sos"), x)


def place(track, sound, at, gain=1.0):
    i = int(at * SR)
    if i >= N:
        return
    seg = sound[: N - i]
    track[i: i + len(seg)] += seg * gain


def env(n, attack, decay):
    t = np.arange(n) / SR
    a = np.clip(t / max(attack, 1e-4), 0, 1)
    return a * np.exp(-t / decay)


def saw(freq, n, detune=0.0):
    t = np.arange(n) / SR
    f = freq * (1 + detune)
    return 2 * ((t * f) % 1.0) - 1


# ---------------- instruments ----------------
def kick():
    n = int(0.45 * SR)
    t = np.arange(n) / SR
    f = 45 + 95 * np.exp(-t / 0.035)
    phase = 2 * np.pi * np.cumsum(f) / SR
    body = np.sin(phase) * np.exp(-t / 0.22)
    click = hp(rng.standard_normal(n), 2500) * np.exp(-t / 0.004) * 0.25
    return np.tanh((body + click) * 1.6)


def clap():
    n = int(0.3 * SR)
    t = np.arange(n) / SR
    noise = bp(rng.standard_normal(n), 900, 5000)
    e = np.exp(-t / 0.09)
    for d in (0.0, 0.011, 0.022):  # three quick slaps
        e += np.where(t >= d, np.exp(-(t - d) / 0.008), 0) * 0.6
    tone = np.sin(2 * np.pi * 190 * t) * np.exp(-t / 0.05) * 0.3
    return (noise * e * 0.5 + tone) * 0.8


def hat(open_=False):
    n = int((0.25 if open_ else 0.06) * SR)
    t = np.arange(n) / SR
    return hp(rng.standard_normal(n), 7000) * np.exp(-t / (0.08 if open_ else 0.018))


def pluck(freq, length=0.22):
    n = int(length * SR)
    x = saw(freq, n, 0.0) * 0.6 + saw(freq, n, 0.006) * 0.4
    x = lp(x, 2600)
    return x * env(n, 0.002, 0.09)


def bass_note(freq, length):
    n = int(length * SR)
    x = saw(freq, n) + 0.5 * np.sin(2 * np.pi * freq / 2 * np.arange(n) / SR)
    x = lp(x, 420, 4)
    e = np.minimum(1, np.arange(n) / (0.004 * SR)) * np.exp(-np.arange(n) / SR / 0.35)
    return np.tanh(x * e * 1.4)


def pad_chord(freqs, length):
    n = int(length * SR)
    x = np.zeros(n)
    for f in freqs:
        for d in (-0.004, 0.0, 0.005):
            x += saw(f, n, d)
    x = lp(x / (len(freqs) * 3), 900, 2)
    a = np.minimum(1, np.arange(n) / (0.6 * SR))
    r = np.minimum(1, (n - np.arange(n)) / (0.6 * SR))
    return x * a * r


def riser(length):
    n = int(length * SR)
    t = np.arange(n) / SR
    noise = rng.standard_normal(n)
    out = np.zeros(n)
    blocks = 40
    for b in range(blocks):  # sweep the cutoff upward block by block
        s, e = b * n // blocks, (b + 1) * n // blocks
        cut = 300 + (9000 - 300) * (b / blocks) ** 2
        out[s:e] = lp(noise[max(0, s - 2000):e], cut)[-(e - s):]
    sweep = np.sin(2 * np.pi * np.cumsum(200 + 1400 * (t / length) ** 2) / SR) * 0.25
    return (out * 0.6 + sweep) * (t / length) ** 2


def impact(big=1.0):
    n = int(2.6 * SR)
    t = np.arange(n) / SR
    f = 30 + 60 * np.exp(-t / 0.08)
    boom = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t / (0.6 * big))
    crash = hp(rng.standard_normal(n), 3000) * np.exp(-t / (0.5 * big)) * 0.35
    thud = lp(rng.standard_normal(n), 200) * np.exp(-t / 0.05) * 0.8
    return np.tanh((boom * 1.2 + crash + thud) * 1.3)


def glitch():
    n = int(0.45 * SR)
    t = np.arange(n) / SR
    sq = np.sign(np.sin(2 * np.pi * 880 * t)) * (np.sin(2 * np.pi * 37 * t) > 0)
    return lp(sq, 4000) * np.exp(-t / 0.18) * 0.35


# ---------------- arrangement ----------------
tracks = {k: np.zeros(N) for k in ("kick", "clap", "hat", "bass", "pluck", "pad", "fx")}
KICK, CLAP = kick(), clap()
HAT_C, HAT_O = hat(), hat(True)

NOTE = {"A1": 55.0, "C2": 65.41, "F1": 43.65, "G1": 49.0,
        "A3": 220.0, "C4": 261.63, "E4": 329.63, "F3": 174.61, "G3": 196.0, "B3": 246.94, "D4": 293.66,
        "A4": 440.0, "C5": 523.25, "E5": 659.25, "G4": 392.0}
PROG = [("A1", ["A3", "C4", "E4"], ["A4", "C5", "E5", "C5"]),
        ("F1", ["F3", "A3", "C4"], ["A4", "C5", "A4", "F3"]),
        ("C2", ["G3", "C4", "E4"], ["G4", "C5", "E5", "C5"]),
        ("G1", ["G3", "B3", "D4"], ["G4", "B3", "D4", "G4"])]


def chord_at(time):
    return PROG[int(time // (2 * 2 * BEAT * 2)) % 4]  # two bars (4 s) per chord


DROP1 = (14.5, 53.0)
DROP2 = (61.0, 84.5)


def in_drop(x):
    return DROP1[0] <= x < DROP1[1] or DROP2[0] <= x < DROP2[1]


steps = np.arange(0, DUR, BEAT / 4)  # 16th-note grid
for s in steps:
    beat_idx = int(round(s / BEAT * 4))
    on_beat = beat_idx % 4 == 0
    beat_in_bar = (beat_idx // 4) % 4
    if s < 14.5:  # intro / problem: ticking hats, a soft pulse from 7 s
        if beat_idx % 2 == 0:
            place(tracks["hat"], HAT_C, s, 0.35 if on_beat else 0.18)
        if 7.0 <= s < 14.0 and on_beat:
            place(tracks["kick"], KICK, s, 0.45)
    elif in_drop(s):
        if on_beat:
            place(tracks["kick"], KICK, s, 1.0)
            if beat_in_bar in (1, 3):
                place(tracks["clap"], CLAP, s, 0.7)
        if beat_idx % 4 == 2:
            place(tracks["hat"], HAT_O, s, 0.28)
        elif beat_idx % 2 == 1:
            place(tracks["hat"], HAT_C, s, 0.22)
        if beat_idx % 2 == 0:  # 8th-note bass
            root = chord_at(s)[0]
            place(tracks["bass"], bass_note(NOTE[root] * (2 if beat_idx % 8 == 6 else 1), BEAT / 2 * 0.95), s, 0.55)
        arp = chord_at(s)[2]
        place(tracks["pluck"], pluck(NOTE[arp[beat_idx % 4]]), s, 0.22)
    elif 53.0 <= s < 61.0:  # breakdown: ticking only, then 16ths tighten
        if beat_idx % (2 if s < 58 else 1) == 0:
            place(tracks["hat"], HAT_C, s, 0.2 + 0.15 * (s - 53) / 8)
    elif s >= 84.5:  # outro: slow kick only
        if on_beat and beat_in_bar == 0 and s < 88:
            place(tracks["kick"], KICK, s, 0.5)

# pads: whole film, quieter in the drops
for start in np.arange(0, DUR, 4.0):
    chord = [NOTE[n] for n in chord_at(start + 0.01)[1]]
    g = 0.5 if (start < 14.5 or 53 <= start < 61 or start >= 84) else 0.32
    place(tracks["pad"], pad_chord(chord, 4.6), start, g)

# effects locked to the edit
place(tracks["fx"], glitch(), 5.0, 0.9)                    # "secrets" glitch
place(tracks["fx"], riser(3.5), 11.0, 0.55)                # into the reveal
place(tracks["fx"], impact(1.2), 14.5, 0.95)               # logo reveal / drop
place(tracks["fx"], riser(2.0), 29.0, 0.25)                # pipeline -> injection
place(tracks["fx"], impact(0.6), 31.0, 0.45)
place(tracks["fx"], impact(0.5), 39.0, 0.35)               # live run
place(tracks["fx"], riser(5.5), 55.5, 0.6)                 # breakdown riser
place(tracks["fx"], impact(1.6), 61.0, 1.1)                # BLOCKED.
place(tracks["fx"], glitch(), 61.02, 0.7)
place(tracks["fx"], riser(3.0), 66.0, 0.2)
place(tracks["fx"], impact(0.7), 69.0, 0.4)                # defense in depth
place(tracks["fx"], impact(0.7), 78.0, 0.4)                # audit log
place(tracks["fx"], impact(1.4), 84.5, 0.85)               # outro logo

# sidechain: duck bass/pad/pluck under every kick
duck = np.ones(N)
kick_env = np.abs(tracks["kick"])
k = lp(kick_env, 12, 1)
duck = 1 - np.clip(k * 1.6, 0, 0.6)
for name in ("bass", "pad", "pluck"):
    tracks[name] *= duck

# silence gap right before BLOCKED (tension), 60.75-61.0
gap = np.ones(N)
i0, i1 = int(60.72 * SR), int(61.0 * SR)
gap[i0:i1] = np.linspace(1, 0.05, i1 - i0)

# simple reverb bus for pad / pluck / fx
ir_n = int(1.8 * SR)
ir = rng.standard_normal(ir_n) * np.exp(-np.arange(ir_n) / SR / 0.45)
ir = lp(ir, 5000) / np.sqrt(np.sum(ir ** 2))
wet_src = tracks["pad"] * 0.6 + tracks["pluck"] * 0.8 + tracks["fx"] * 0.35 + tracks["clap"] * 0.3
wet = fftconvolve(wet_src, ir)[:N] * 0.35

mix_l = (tracks["kick"] * 0.95 + tracks["clap"] * 0.55 + tracks["bass"] * 0.9 + tracks["pad"] * 0.8
         + tracks["fx"] * 0.9 + wet)
mix_r = mix_l.copy()
# stereo width: hats and plucks slightly panned, delay on plucks
delay = int(BEAT * 0.75 * SR)
pl = tracks["pluck"]
pl_d = np.zeros(N)
pl_d[delay:] = pl[:-delay] * 0.35
mix_l += tracks["hat"] * 0.42 + pl * 0.7 + pl_d * 0.2
mix_r += tracks["hat"] * 0.30 + pl * 0.5 + pl_d * 0.6
mix = np.stack([mix_l, mix_r], axis=1) * gap[:, None]

# master: gentle low cut, soft clip, fade in/out, normalize to -1 dBFS peak
mix = np.stack([hp(mix[:, 0], 28), hp(mix[:, 1], 28)], axis=1)
mix = np.tanh(mix * 1.2) / np.tanh(1.2)
fade_in = np.minimum(1, t_all / 0.4)
fade_out = np.clip((DUR - t_all) / 2.5, 0, 1)
mix *= (fade_in * fade_out)[:, None]
mix *= 10 ** (-1 / 20) / np.max(np.abs(mix))

wavfile.write(sys.argv[1] if len(sys.argv) > 1 else "music.wav", SR, (mix * 32767).astype(np.int16))
print("wrote", sys.argv[1] if len(sys.argv) > 1 else "music.wav", f"{DUR}s")
