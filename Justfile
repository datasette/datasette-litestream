dev: 
  watchexec --signal SIGKILL --restart --clear -e py,ts,html,js,css,yaml -- \
  python3 -m datasette --root --plugins-dir=./src/datasette_litestream demo/*.db --metadata=demo/metadata.yaml -p 8002
