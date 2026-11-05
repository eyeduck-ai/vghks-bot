"use strict";
window.SOAPView = (() => {
  const textFields = ["subjective", "objective", "assessment_plan", "assessment", "plan"];
  const make = (tag, text, cls) => {const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
  function marked(text, ranges=[], tag="pre", cls="soap") {
    const output=make(tag,undefined,cls), chars=Array.from(text||""), merged=[];
    for(const [start,end,category,kind] of ranges.filter(([s,e])=>Number.isInteger(s)&&Number.isInteger(e)&&s>=0&&e>s&&e<=chars.length).sort((a,b)=>a[0]-b[0])){
      const previous=merged.at(-1);
      if(previous&&start<=previous.end){
        previous.end=Math.max(previous.end,end);
        if(category)previous.categories.add(category);
        if(kind==="search")previous.search=true;
      }else merged.push({start,end,categories:new Set(category?[category]:[]),search:kind==="search"});
    }
    let offset=0;
    for(const {start,end,categories,search} of merged){
      const mark=make("mark",chars.slice(start,end).join(""));mark.tabIndex=-1;
      if(categories.size)mark.dataset.tagCategories=JSON.stringify([...categories]);
      if(search)mark.dataset.searchHit="true";
      output.append(document.createTextNode(chars.slice(offset,start).join("")),mark);offset=end;
    }
    output.append(document.createTextNode(chars.slice(offset).join("")));return output;
  }
  function fullRanges(record){return [...(record.matches||[]).map(m=>[m.start,m.keyword_end,m.category]),...(record.highlights||[]).map(([start,end])=>[start,end,null,"search"])];}
  function searchRanges(record,text){
    if(!record.highlights?.length||!text)return [];
    const source=Array.from(record.soap||""),terms=new Set(record.highlights.map(([start,end])=>source.slice(start,end).join("")).filter(term=>term.trim()));
    const ranges=[];
    for(const term of terms){
      const pattern=term.replace(/[.*+?^${}()|[\]\\]/g,"\\$&");
      for(const match of text.matchAll(new RegExp(pattern,"giu"))){
        const start=Array.from(text.slice(0,match.index)).length;
        ranges.push([start,start+Array.from(match[0]).length,null,"search"]);
      }
    }
    return ranges;
  }
  function fieldRanges(record,field,text){
    return [...(record.matches||[]).filter(m=>m.source_field===field&&m.source_index===null).map(m=>[m.source_start,m.source_keyword_end,m.category]),...searchRanges(record,text)];
  }
  function issueNote(record){
    const issues=record.tag_scope_issues||[];
    if(!issues.length)return null;
    return make("p","tag 判定不完整："+issues.map(t=>`${t.name}（${t.scope_name} ${t.status==="partial"?"解析不完整":"未取得可辨識區塊"}）`).join("、"),"soap-notice");
  }
  function section(title){const n=make("section",undefined,"soap-section");n.append(make("h4",title));return n;}
  function textSection(record,title,fields){
    const out=section(title), data=record.soap_structure;
    const identified=fields.filter(field=>typeof data[field]==="string");
    if(!identified.length)out.append(make("p","未辨識此區塊","caption"));
    for(const field of identified){
      if(field==="assessment"||field==="plan")out.append(make("span",field==="assessment"?"A":"P","soap-subheading"));
      const value=data[field].trim();
      out.append(value?marked(value,fieldRanges(record,field,value)):make("p","此區塊內容空白","caption"));
    }
    return out;
  }
  function cellRanges(text,hits){
    const ranges=[];
    for(const hit of hits){
      const pattern=hit.keyword.replace(/[.*+?^${}()|[\]\\]/g,"\\$&");
      for(const match of text.matchAll(new RegExp(pattern,"giu"))){
        const start=Array.from(text.slice(0,match.index)).length;
        ranges.push([start,start+Array.from(match[0]).length]);
      }
    }
    return ranges;
  }
  function periodDescription(period){
    if(!period)return "";
    const parts=[];
    if(period.start_date&&period.end_date)parts.push(`${period.source_label||"服藥期限"}：${period.start_date} ～ ${period.end_date}`);
    if(period.printed_months)parts.push(`院方標示 ${period.printed_months} 個月`);
    if(period.printed_period_count)parts.push(`院方標示 ${period.printed_period_count} 期`);
    if(period.printed_dispense_count)parts.push(`院方標示分 ${period.printed_dispense_count} 次`);
    if(!period.printed_months&&!period.printed_period_count&&!period.printed_dispense_count&&period.estimated_cycles&&period.estimated_cycle_days)
      parts.push(`依期限推估 ${period.estimated_cycles} 個 ${period.estimated_cycle_days} 天週期`);
    return parts.join(" · ");
  }
  function summaryTable(record,field,title,columns,sectionCode){
    const out=section(title), data=record.soap_structure, rows=data[field]||[];
    const medication=field==="medications",periods=medication?data.chronic_prescription_periods||[]:[];
    const hasChronic=medication&&rows.some(row=>row.prescription_type==="chronic");
    const linkedPeriods=new Set(hasChronic?rows.filter(row=>row.prescription_type==="chronic").map(row=>row.chronic_period_index):[]);
    periods.forEach((period,index)=>{
      const description=periodDescription(period);
      if(description&&!linkedPeriods.has(index))out.append(make("p",`慢性處方箋 · ${description}`,"soap-period"));
    });
    if(!rows.length){
      const known=(data.present_sections||[]).includes(sectionCode);
      const failed=(data.parsing_issues||[]).some(code=>code.startsWith(field==="medications"?"SOAP_MEDICATION_":field==="orders"?"SOAP_ORDER_":"SOAP_DIAGNOSIS_"));
      out.append(make("p",failed?"此摘要尚未完整解析，請查看完整原文。":known?"院方此摘要沒有資料列。":"未辨識此摘要。","caption"));return out;
    }
    const wrap=make("div",undefined,"table-wrap"),table=make("table",undefined,"soap-table"),head=make("thead"),hr=make("tr");
    for(const [,label] of columns){const th=make("th",label);th.scope="col";hr.append(th);}head.append(hr);
    table.append(head);
    let body,previousGroup;
    rows.forEach((row,index)=>{
      const chronic=hasChronic&&row.prescription_type==="chronic";
      const group=chronic?`chronic:${row.chronic_period_index}`:row.prescription_type||"legacy";
      if(!body||hasChronic&&group!==previousGroup){
        body=make("tbody");table.append(body);previousGroup=group;
        const label=chronic?"慢性處方箋":hasChronic&&group==="regular"?"一般處方":"";
        if(label){
          const tr=make("tr"),th=make("th",label,"soap-prescription-group");
          th.scope="rowgroup";th.colSpan=columns.length;
          const period=chronic?periods[row.chronic_period_index]:null,description=periodDescription(period);
          if(description)th.append(make("span",description,"soap-prescription-period"));
          tr.append(th);body.append(tr);
        }
      }
      const tr=make("tr"),hits=(record.matches||[]).filter(m=>m.source_field===field&&m.source_index===index);
      if(hits.length){tr.className="soap-tag-row";tr.tabIndex=-1;tr.dataset.tagCategories=JSON.stringify([...new Set(hits.map(hit=>hit.category))]);}
      for(const [key] of columns){const td=make("td"),value=String(row[key]??"");td.append(marked(value,[...cellRanges(value,hits),...searchRanges(record,value)],"span","soap-cell"));tr.append(td);}
      if(hits.length&&!tr.querySelector("mark")){const badge=make("mark","tag","soap-row-hit");badge.tabIndex=-1;badge.title="tag 命中於此原始資料列";tr.firstElementChild.prepend(badge);}
      body.append(tr);
    });
    wrap.append(table);out.append(wrap);return out;
  }
  function create(record){
    const out=make("div",undefined,"soap-view"),data=record.soap_structure,warning=issueNote(record);
    if(warning)out.append(warning);
    const structured=data&&((data.present_sections||[]).length||textFields.some(field=>typeof data[field]==="string"));
    if(!structured){
      out.append(make("p",data?"此份 SOAP 未辨識到結構化區塊，以下顯示完整原文。":"此份病歷未保存結構資料；在病歷檢閱選擇「重新下載病歷」可重新取得。","soap-notice"),marked(record.soap,fullRanges(record)));
      return out;
    }
    if(data.parsing_issues?.length){
      const issue=make("details",undefined,"soap-issues");issue.append(make("summary","部分資料尚未完整解析，請核對原文"),make("p",data.parsing_issues.join(" · "),"caption"));out.append(issue);
    }
    out.append(textSection(record,"A+P · 評估與計畫",["assessment_plan","assessment","plan"]),textSection(record,"O · 客觀資料",["objective"]),textSection(record,"S · 主訴",["subjective"]));
    out.append(summaryTable(record,"diagnoses","診斷",[["coding_system","分類"],["code","代碼"],["name","診斷名稱"]],"DIAGNOSES"));
    out.append(summaryTable(record,"medications","藥囑",[["name","藥名"],["dose","劑量"],["unit","單位"],["route","途徑"],["frequency","頻次"],["days","天數"],["total_quantity","總發藥量"]],"MEDICATIONS"));
    out.append(summaryTable(record,"orders","醫囑",[["name","醫囑名稱"],["quantity","數量"]],"ORDERS"));
    if(data.unclassified_blocks?.length){const other=make("details",undefined,"soap-original");other.append(make("summary","其他原文"));for(const block of data.unclassified_blocks)other.append(marked(block));out.append(other);}
    const raw=make("details",undefined,"soap-original");raw.append(make("summary","完整原文"),marked(record.soap,fullRanges(record)));raw.open=!!record.highlights?.length;out.append(raw);
    return out;
  }
  function reveal(mark){for(let parent=mark.parentElement;parent;parent=parent.parentElement)if(parent.tagName==="DETAILS")parent.open=true;mark.scrollIntoView({block:"center"});mark.focus({preventScroll:true});}
  return {create,reveal};
})();
