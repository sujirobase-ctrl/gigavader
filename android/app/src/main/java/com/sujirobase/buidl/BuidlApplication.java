package com.sujirobase.buidl;

import android.app.Application;

public class BuidlApplication extends Application {
    public AppModel model;
    @Override public void onCreate() {
        super.onCreate();
        model = new AppModel(this);
    }
}
