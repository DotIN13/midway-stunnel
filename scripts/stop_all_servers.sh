#!/bin/bash

# Kill all scode and vscode processes for the current user
pkill -u $USER -f scode
pkill -u $USER -f vscode
