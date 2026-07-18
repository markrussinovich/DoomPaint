"""Smoke test: engine boots headless, produces frames, accepts actions."""
import os

import numpy as np
import vizdoom as vzd
from PIL import Image

game = vzd.DoomGame()
game.set_doom_game_path(os.path.join(vzd.scenarios_path, "..", "freedoom1.wad"))
game.set_doom_map("E1M1")
game.set_screen_resolution(vzd.ScreenResolution.RES_640X400)
game.set_screen_format(vzd.ScreenFormat.RGB24)
game.set_window_visible(False)
game.set_mode(vzd.Mode.PLAYER)
game.set_render_hud(True)
game.set_render_weapon(True)
game.set_episode_timeout(0)
for b in (vzd.Button.MOVE_FORWARD, vzd.Button.TURN_LEFT, vzd.Button.ATTACK):
    game.add_available_button(b)
game.init()

frames = []
for i in range(35):  # one second of game time
    if game.is_episode_finished():
        game.new_episode()
    frames.append(game.get_state().screen_buffer.copy())
    game.make_action([1, 0, 0])  # walk forward

game.close()

first, last = frames[0], frames[-1]
print("frame shape:", first.shape, first.dtype)
print("frames differ:", not np.array_equal(first, last))
Image.fromarray(last).save(r"C:\Source\MsPaintDoom\smoke_frame.png")
print("saved smoke_frame.png")
