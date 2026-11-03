import re
import subprocess
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

class SecurityContracts(unittest.TestCase):
    def test_actions_are_pinned_and_tag_is_never_shell_source(self):
        for name in ('ci.yml','release.yml'):
            text=(ROOT/'.github/workflows'/name).read_text(encoding='utf-8')
            actions=re.findall(r'^\s*uses:\s*(\S+)|^\s*-\s*uses:\s*(\S+)',text,re.M)
            for groups in actions:
                action=next(part for part in groups if part)
                if action.startswith('./'): continue
                self.assertRegex(action,r'^[\w-]+/[\w-]+@[0-9a-f]{40}$')
        release=(ROOT/'.github/workflows/release.yml').read_text(encoding='utf-8')
        for line in release.splitlines():
            if 'github.ref_name' in line:
                self.assertRegex(line.strip(),r'^(run-name:|RELEASE_TAG:)')
        self.assertIn('--tag "$RELEASE_TAG"',release)
        self.assertIn('gh release edit "$RELEASE_TAG"',release)

    def test_explorer_sends_auth_only_as_an_explicit_header(self):
        source=(ROOT/'web/app.html').read_text(encoding='utf-8')
        start=source.index('    let authorization')
        end=source.index('    function abortHistoryRequests',start)
        script=source[start:end]
        # Run the page's actual request helper with a fetch stub, without a DOM.
        probe='''const assert=require('node:assert/strict');
let calls=[];
global.fetch=async(path,options)=>{calls.push([path,options]);return {ok:true,status:200,json:async()=>({token:'example-token'})};};
(async()=>{SCRIPT
let auth=await request('/auth',{method:'POST',headers:{'X-Agrep-Bootstrap':'one-use'}});
assert.equal(calls[0][1].credentials,'omit');assert.equal(calls[0][1].headers.has('Authorization'),false);
authorization=auth.token;
await request('/api/chats',{credentials:'include'});
assert.equal(calls[1][1].headers.get('Authorization'),'Bearer example-token');
assert.equal(calls[1][1].credentials,'omit');assert.equal(calls[1][1].mode,'same-origin');
})().catch(e=>{console.error(e);process.exitCode=1;});'''.replace('SCRIPT',script)
        result=subprocess.run(['node','-e',probe],capture_output=True,text=True,encoding='utf-8')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertNotIn('sessionStorage',source)
        self.assertNotIn('document.cookie',source)
