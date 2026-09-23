const path = require('node:path');

// Source-code links open GitHub, not file downloads rewritten as page routes.
module.exports = function sourceLinks() {
  return function transform(tree) {
    function walk(node) {
      if (['link', 'definition'].includes(node.type) && /^\.\.\/(scripts|schemas|examples)\//.test(node.url)) {
        const [file, fragment] = node.url.slice(3).split('#');
        const target = path.posix.normalize(file);
        if (!/^(scripts|schemas|examples)\//.test(target)) {
          throw new Error(`Source link escapes public directories: ${node.url}`);
        }
        node.url = 'https://github.com/turbra/traceonaut/blob/main/' + target
          + (fragment ? '#' + fragment : '');
      }
      for (const child of node.children || []) walk(child);
    }
    walk(tree);
  };
};
