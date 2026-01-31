#!/bin/bash
echo 'Cleaning up test resources...'

# Kill any lingering VSCode processes
pkill -f 'code.*test-data-dir' 2>/dev/null || true

# Clear test data directory
rm -rf /home/rwurmbra/Desktop/rootcause_folders/editor-extensions/tests/test-data-dir/* 2>/dev/null || true

# Remove cloned repos
rm -rf /home/rwurmbra/Desktop/rootcause_folders/editor-extensions/tests/coolstore 2>/dev/null || true
rm -rf /home/rwurmbra/Desktop/rootcause_folders/editor-extensions/tests/inventory_management 2>/dev/null || true

echo 'Cleanup complete'
exit 0
