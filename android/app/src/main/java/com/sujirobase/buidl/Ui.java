package com.sujirobase.buidl;

import android.app.Activity;
import android.content.Context;
import android.content.res.ColorStateList;
import android.content.res.Configuration;
import android.graphics.*;
import android.graphics.drawable.*;
import android.view.View;
import android.widget.*;
import androidx.core.view.ViewCompat;

/** Shared native UI tokens; text remains in sp and controls can grow with it. */
public final class Ui {
    public final Context context;
    public final boolean dark;
    public final int bg, surface, field, text, muted, accent, onAccent, line, success, error;
    public Ui(Context context) {
        this.context=context; dark=isDark(context);
        bg=Color.parseColor(dark ? "#10111B" : "#F5F4FA");
        surface=Color.parseColor(dark ? "#1B1C2B" : "#FFFFFF");
        field=Color.parseColor(dark ? "#252638" : "#F0EFF7");
        text=Color.parseColor(dark ? "#F6F4FF" : "#222138");
        muted=Color.parseColor(dark ? "#B8B6CC" : "#615E77");
        accent=Color.parseColor(dark ? "#B5A4FF" : "#5C43D6");
        onAccent=Color.parseColor(dark ? "#211643" : "#FFFFFF");
        line=Color.parseColor(dark ? "#858199" : "#77718B");
        success=Color.parseColor(dark ? "#86DFBD" : "#17674F");
        error=Color.parseColor(dark ? "#FFADBB" : "#A12342");
    }
    public static boolean isDark(Context context) {
        int choice=context.getSharedPreferences("buidl",Context.MODE_PRIVATE).getInt("appearance",2);
        return choice==2 || (choice==0 && (context.getResources().getConfiguration().uiMode & Configuration.UI_MODE_NIGHT_MASK)==Configuration.UI_MODE_NIGHT_YES);
    }
    public static void applyTheme(Activity activity) { activity.setTheme(isDark(activity) ? R.style.AppTheme : R.style.AppTheme_Light); }
    public void window(Activity activity) {
        activity.getWindow().setStatusBarColor(bg); activity.getWindow().setNavigationBarColor(bg);
        activity.getWindow().getDecorView().setSystemUiVisibility(dark ? 0 : View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR|View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR);
    }
    public int dp(int value) { return Math.round(value*context.getResources().getDisplayMetrics().density); }
    public GradientDrawable shape(int color,int radius,int strokeColor,int stroke) {
        GradientDrawable shape=new GradientDrawable();shape.setColor(color);shape.setCornerRadius(dp(radius));
        if(stroke>0)shape.setStroke(dp(stroke),strokeColor);return shape;
    }
    private Drawable interactive(int color) {
        StateListDrawable states=new StateListDrawable();
        states.addState(new int[]{-android.R.attr.state_enabled},shape(field,14,line,1));
        states.addState(new int[]{android.R.attr.state_focused},shape(color,14,accent,3));
        states.addState(new int[]{},shape(color,14,line,1));
        return new RippleDrawable(ColorStateList.valueOf(dark ? 0x33FFFFFF : 0x22000000),states,null);
    }
    public void styleButton(Button button,boolean primary) {
        button.setAllCaps(false);button.setTextSize(15);button.setTypeface(Typeface.create("sans-serif-medium",Typeface.NORMAL));
        button.setMinHeight(dp(52));button.setMinimumHeight(dp(52));button.setMinWidth(dp(48));button.setMinimumWidth(dp(48));
        button.setPadding(dp(14),dp(12),dp(14),dp(12));button.setGravity(android.view.Gravity.CENTER);
        button.setBackgroundTintList(null);button.setBackground(interactive(primary ? accent : field));
        button.setTextColor(new ColorStateList(new int[][]{new int[]{-android.R.attr.state_enabled},new int[]{}},
                new int[]{muted,primary ? onAccent : text}));
        button.setStateListAnimator(null);
    }
    public Button button(String label,boolean primary,Runnable action) {
        Button result=new Button(context);styleButton(result,primary);result.setText(label);result.setOnClickListener(v -> action.run());
        LinearLayout.LayoutParams p=new LinearLayout.LayoutParams(-1,-2);p.setMargins(0,dp(4),0,dp(4));result.setLayoutParams(p);return result;
    }
    public TextView text(String value,int size,int color) {
        TextView result=new TextView(context);result.setText(value);result.setTextSize(Math.max(size,14));result.setTextColor(color);
        result.setPadding(0,dp(3),0,dp(5));result.setLineSpacing(dp(2),1f);return result;
    }
    public TextView heading(String value,int size) {
        TextView result=text(value,size,text);result.setTypeface(Typeface.create("sans-serif-medium",Typeface.NORMAL));
        ViewCompat.setAccessibilityHeading(result,true);return result;
    }
    public EditText field(String hint,String value) {
        EditText result=new EditText(context);result.setTextColor(text);result.setHintTextColor(muted);result.setTextSize(16);
        result.setSingleLine(true);result.setHint(hint);result.setText(value);result.setMinHeight(dp(56));
        result.setPadding(dp(14),dp(12),dp(14),dp(12));result.setBackground(interactive(field));return result;
    }
    public CheckBox checkbox(String label,boolean selected) {
        CheckBox box=new CheckBox(context);box.setText(label);box.setTextColor(text);box.setTextSize(15);box.setChecked(selected);
        box.setMinHeight(dp(48));box.setMinimumWidth(dp(48));box.setPadding(dp(2),dp(6),dp(4),dp(6));
        box.setButtonTintList(new ColorStateList(new int[][]{new int[]{android.R.attr.state_checked},new int[]{}},new int[]{accent,line}));return box;
    }
    public TextView label(String title,View control) {
        if(control.getId()==View.NO_ID)control.setId(View.generateViewId());
        TextView label=text(title,14,muted);label.setLabelFor(control.getId());return label;
    }
    public void spinner(Spinner spinner,String[] labels) {
        spinner.setMinimumHeight(dp(52));spinner.setBackground(interactive(field));spinner.setPadding(dp(10),0,dp(8),0);
        spinner.setAdapter(new ArrayAdapter<String>(context,android.R.layout.simple_spinner_dropdown_item,labels) {
            @Override public View getView(int position,View convert,android.view.ViewGroup parent) {return item(position);}
            @Override public View getDropDownView(int position,View convert,android.view.ViewGroup parent) {return item(position);}
            private TextView item(int position) {TextView v=text(getItem(position),16,text);v.setMinHeight(dp(52));v.setGravity(android.view.Gravity.CENTER_VERTICAL);v.setPadding(dp(12),dp(10),dp(12),dp(10));v.setBackgroundColor(surface);return v;}
        });
    }
    public Drawable icon(String name,int color) { return new Icon(name,color,dp(24)); }
    public static void key(View view,String key) {
        view.setTag(key);
        int id=view.getResources().getIdentifier(key,"id",view.getContext().getPackageName());
        if(id!=0)view.setId(id);
    }
    public static boolean hasKey(View view,String key) { return key.equals(view.getTag()); }

    private static final class Icon extends Drawable {
        final Paint paint=new Paint(Paint.ANTI_ALIAS_FLAG);final String name;final int size;
        Icon(String name,int color,int size) {this.name=name;this.size=size;paint.setColor(color);paint.setStyle(Paint.Style.STROKE);paint.setStrokeWidth(1.8f);paint.setStrokeCap(Paint.Cap.ROUND);paint.setStrokeJoin(Paint.Join.ROUND);}
        @Override public void draw(Canvas c) {
            int save=c.save();c.translate(getBounds().left,getBounds().top);c.scale(getBounds().width()/24f,getBounds().height()/24f);
            Path p=new Path();
            switch(name) {
                case "grid": for(int x:new int[]{3,14})for(int y:new int[]{3,14})c.drawRoundRect(x,y,x+7,y+7,1.5f,1.5f,paint);break;
                case "folder": p.moveTo(3,7);p.lineTo(3,5);p.lineTo(9,5);p.lineTo(11,8);p.lineTo(21,8);p.lineTo(21,20);p.lineTo(3,20);p.close();c.drawPath(p,paint);break;
                case "palette": c.drawCircle(12,12,9,paint);c.drawCircle(8,9,1,paint);c.drawCircle(14,7,1,paint);c.drawCircle(17,13,1,paint);c.drawCircle(10,17,1,paint);break;
                case "shield": p.moveTo(12,3);p.lineTo(20,6);p.lineTo(19,14);p.quadTo(17,19,12,22);p.quadTo(7,19,5,14);p.lineTo(4,6);p.close();c.drawPath(p,paint);c.drawLine(9,12,11,14,paint);c.drawLine(11,14,16,9,paint);break;
                case "search": c.drawCircle(10,10,6,paint);c.drawLine(15,15,21,21,paint);break;
                case "play": p.moveTo(8,5);p.lineTo(19,12);p.lineTo(8,19);p.close();c.drawPath(p,paint);break;
                default: c.drawLine(12,3,12,15,paint);c.drawLine(7,10,12,15,paint);c.drawLine(12,15,17,10,paint);p.moveTo(4,16);p.lineTo(4,21);p.lineTo(20,21);p.lineTo(20,16);c.drawPath(p,paint);
            }c.restoreToCount(save);
        }
        @Override public int getIntrinsicWidth(){return size;}@Override public int getIntrinsicHeight(){return size;}
        @Override public void setAlpha(int alpha){paint.setAlpha(alpha);}@Override public void setColorFilter(ColorFilter filter){paint.setColorFilter(filter);}
        @Override public int getOpacity(){return PixelFormat.TRANSLUCENT;}
    }
}
