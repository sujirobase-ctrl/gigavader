package com.sujirobase.buidl;

import org.mozilla.javascript.Context;
import org.mozilla.javascript.ContextFactory;
import org.mozilla.javascript.Scriptable;

/** No Java bindings, DOM, network, cookies or files are exposed to remote JS. */
public final class SaltRuntime {
    public String evaluate(String script) {
        if(script.length()>2*1024*1024)throw new IllegalArgumentException("Script excede limite");
        final long deadline=System.nanoTime()+5_000_000_000L;
        ContextFactory factory=new ContextFactory(){
            @Override protected Context makeContext(){Context c=super.makeContext();c.setOptimizationLevel(-1);c.setInstructionObserverThreshold(10000);c.setClassShutter(name -> false);return c;}
            @Override protected void observeInstructionCount(Context c,int count){if(System.nanoTime()>deadline)throw new IllegalStateException("Tempo limite do script");}
        };
        return factory.call(c -> {
            Scriptable scope=c.initSafeStandardObjects();
            c.evaluateString(scope,"var navigator={userAgent:'probe',language:'en-US'};", "probe",1,null);
            c.evaluateString(scope,script,"website-token",1,null);
            return Context.toString(c.evaluateString(scope,"_sha256=function(x){return x;};generateWT('TOKEN');","probe",1,null));
        });
    }
}
