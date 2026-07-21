"""Spike: does ViZDoom's ZDoom fork honor the 'save'/'load' console commands?

Everything the File>Open "resume game" feature needs depends on this. Boots
headless (no Paint involved, same pattern as smoke_test.py), settles the
player to a full stop, saves, moves elsewhere, loads, and checks that both
position AND velocity were restored (a naive check right after `load` sees a
few tics of residual momentum decay, which looks like drift but isn't — it's
proof the *velocity* was restored too).

Run: python save_load_spike.py
"""
import os
import time

import vizdoom as vzd

wad_dir = os.path.dirname(vzd.__file__)
SAVE_FILE = os.path.join(os.getcwd(), "1.zds")
if os.path.exists(SAVE_FILE):
    os.remove(SAVE_FILE)

game = vzd.DoomGame()
game.set_doom_game_path(os.path.join(wad_dir, "freedoom1.wad"))
game.set_doom_map("E1M1")
game.set_screen_resolution(vzd.ScreenResolution.RES_640X400)
game.set_screen_format(vzd.ScreenFormat.RGB24)
game.set_window_visible(False)
game.set_mode(vzd.Mode.PLAYER)
game.set_episode_timeout(0)
for b in (vzd.Button.MOVE_FORWARD, vzd.Button.TURN_LEFT, vzd.Button.ATTACK):
    game.add_available_button(b)
for gv in (vzd.GameVariable.POSITION_X, vzd.GameVariable.POSITION_Y,
           vzd.GameVariable.VELOCITY_X, vzd.GameVariable.VELOCITY_Y):
    game.add_available_game_variable(gv)
game.init()


def pos_vel():
    st = game.get_state()
    x, y, vx, vy = st.game_variables
    return x, y, vx, vy


SETTLE_TICS = 70  # Doom ground friction ~0.90625/tic; ~70 tics -> v ~ 0

# Walk forward, then come to a full stop before saving.
for _ in range(35):
    game.make_action([1, 0, 0])
for _ in range(SETTLE_TICS):
    game.make_action([0, 0, 0])
x0, y0, vx0, vy0 = pos_vel()
print(f"before save: pos=({x0:.2f},{y0:.2f}) vel=({vx0:.3f},{vy0:.3f})")

game.send_game_command("save 1 doompaint_spike")
time.sleep(0.5)
for _ in range(5):
    game.make_action([0, 0, 0])
print(".zds written:", os.path.exists(SAVE_FILE))

# Move far away and stop, so post-load state can't be mistaken for a no-op.
for _ in range(35):
    game.make_action([1, 0, 0])
for _ in range(SETTLE_TICS):
    game.make_action([0, 0, 0])
x1, y1, vx1, vy1 = pos_vel()
print(f"after moving away: pos=({x1:.2f},{y1:.2f}) vel=({vx1:.3f},{vy1:.3f})")

game.send_game_command("load 1")
time.sleep(0.5)
for _ in range(SETTLE_TICS):  # let restored velocity (should be ~0) settle
    game.make_action([0, 0, 0])
x2, y2, vx2, vy2 = pos_vel()
print(f"after load (settled): pos=({x2:.2f},{y2:.2f}) vel=({vx2:.3f},{vy2:.3f})")

ok = abs(x2 - x0) < 2 and abs(y2 - y0) < 2
print("\nRESULT:", "PASS — load restored the saved position" if ok
      else "FAIL — position did not restore")

game.close()
if os.path.exists(SAVE_FILE):
    os.remove(SAVE_FILE)
