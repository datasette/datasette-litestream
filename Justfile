dev: 
  watchexec --signal SIGKILL --restart --clear -e py,ts,html,js,css,yaml -- \
  python3 -m datasette --root --plugins-dir=./datasette_litestream *.db --metadata=metadata.yaml -p 8002
