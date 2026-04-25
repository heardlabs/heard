"""TTS backends. Imports are lazy on purpose — touching this package
must not pull in a heavy runtime. Each backend module is imported by
its specific user (daemon, cli, tune)."""
