#!/bin/zsh
# macOS launcher for Spektrafilm LUT Generator.
#
# By default this activates a conda/miniforge environment named "Spektrafilm".
# To use a different environment name without editing this file, run:
#   SPEKTRAFILM_CONDA_ENV="your-env-name" ./launch_mac.command

export TK_SILENCE_DEPRECATION=1

SCRIPT_DIR="${0:A:h}"
cd "$SCRIPT_DIR" || exit 1

ENV_NAME="${SPEKTRAFILM_CONDA_ENV:-Spektrafilm}"

# Load conda/miniforge for non-interactive zsh launchers.
if [ -f "$HOME/miniforge3/etc/profile.d/conda.sh" ]; then
  source "$HOME/miniforge3/etc/profile.d/conda.sh"
elif [ -f "$HOME/miniconda3/etc/profile.d/conda.sh" ]; then
  source "$HOME/miniconda3/etc/profile.d/conda.sh"
elif [ -f "$HOME/anaconda3/etc/profile.d/conda.sh" ]; then
  source "$HOME/anaconda3/etc/profile.d/conda.sh"
else
  echo "Could not find conda.sh."
  echo "Open Terminal and run:"
  echo "  conda activate $ENV_NAME"
  echo "  python spektrafilm_state_to_lut_gui.py"
  read "?Press Return to close..."
  exit 1
fi

if ! conda activate "$ENV_NAME"; then
  echo "Could not activate conda environment: $ENV_NAME"
  echo "Edit launch_mac.command, or run with:"
  echo "  SPEKTRAFILM_CONDA_ENV=your-env-name ./launch_mac.command"
  read "?Press Return to close..."
  exit 1
fi

python "$SCRIPT_DIR/spektrafilm_state_to_lut_gui.py"
