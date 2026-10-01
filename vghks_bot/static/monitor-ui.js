"use strict";
window.MonitorUI={fields(current){
  const element=node("div",undefined,"frequency-fields"),label=node("label","每隔"),input=node("input"),unitLabel=node("label","單位"),unit=node("select");
  input.type="number";input.min="1";input.step="1";input.required=true;unit.append(new Option("小時","hours"),new Option("天","days"));
  unit.value=current.interval_unit||(current.hours%24===0?"days":"hours");input.value=current.interval_value||(unit.value==="days"?current.hours/24:current.hours);
  function maximum(){input.max=unit.value==="days"?"30":"720";}unit.addEventListener("change",maximum);maximum();label.append(input);unitLabel.append(unit);element.append(label,unitLabel);Choices.enhance(unit);
  return {element,value:()=>({interval_value:Number(input.value),interval_unit:unit.value})};
}};
